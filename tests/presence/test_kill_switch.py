"""v0.5.4 kill switch — comprehensive test suite.

Covers:
  * Happy path (alive vs killed, assert_alive metadata)
  * Cache TTL behaviour (hit, miss, force refresh, lock under concurrency)
  * Invariant #1 (DB outage resilience — never crashes the host)
  * In-memory mode (no engine — derive from _agents dict)
  * Edge cases (missing agent, unicode IDs, long IDs, empty metadata)
  * Multi-agent scaling (large kill set)
  * Re-kill / un-kill semantics
  * SDK-level wiring via ScopeModule.set_presence_module

DB-backed integration is in tests/presence/test_kill_switch_postgres.py
(marked @pytest.mark.postgres, skipped without GOVERNANCE_TEST_DB_URL).

The tests here use the in-memory PresenceModule (no engine). The kill
cache then derives from `_agents[*]["metadata"]["_killed_by"]`, exercising
the same `_derive_killed_from_memory()` code path that runs when
`_get_engine()` returns None.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from codeatelier_governance.presence import (
    AgentKilledError,
    AgentStatus,
    PresenceModule,
)
from codeatelier_governance.presence import module as presence_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _kill_agent_in_memory(
    presence: PresenceModule,
    agent_id: str,
    *,
    killed_by: str = "test-operator",
    reason: str = "test",
    killed_at: str | None = None,
) -> None:
    """Simulate the console kill_agent endpoint in the in-memory store.

    Mirrors what `console/app.py:1788-1802` does: writes `_killed_by`,
    `_killed_at`, `_kill_reason` into metadata and sets status to
    'unresponsive'. The presence module's kill-cache reads the metadata
    fields, NOT the status (status is overloaded with stale-heartbeat).
    """
    if killed_at is None:
        killed_at = datetime.now(timezone.utc).isoformat()
    async with presence._lock:
        if agent_id not in presence._agents:
            raise RuntimeError(
                f"agent {agent_id!r} not in presence; call heartbeat() first"
            )
        meta = presence._agents[agent_id].setdefault("metadata", {})
        meta["_killed_by"] = killed_by
        meta["_killed_at"] = killed_at
        meta["_kill_reason"] = reason
        presence._agents[agent_id]["status"] = AgentStatus.UNRESPONSIVE.value


# ---------------------------------------------------------------------------
# 1. AgentKilledError construction
# ---------------------------------------------------------------------------


def test_agent_killed_error_minimal() -> None:
    """AgentKilledError can be constructed with just agent_id."""
    err = AgentKilledError("agent-x")
    assert err.agent_id == "agent-x"
    assert err.killed_by is None
    assert err.killed_at is None
    assert err.reason is None
    assert "agent-x" in str(err)
    assert "fail-closed" in str(err)


def test_agent_killed_error_full_metadata() -> None:
    """AgentKilledError surfaces all metadata fields in str()."""
    err = AgentKilledError(
        "agent-y",
        killed_by="leo",
        killed_at="2026-04-14T22:00:00",
        reason="incident-001",
    )
    s = str(err)
    assert "agent-y" in s
    assert "leo" in s
    assert "2026-04-14T22:00:00" in s
    assert "incident-001" in s


def test_agent_killed_error_is_runtime_error() -> None:
    """AgentKilledError is a RuntimeError so generic except clauses catch it."""
    err = AgentKilledError("a")
    assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# 2. Happy path — alive vs killed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_killed_returns_false_for_live_agent(
    presence: PresenceModule,
) -> None:
    """A live agent (heartbeat-only, no kill marker) is not killed."""
    await presence.heartbeat("agent-1")
    assert await presence.is_killed("agent-1") is False


@pytest.mark.asyncio
async def test_is_killed_returns_true_after_kill(
    presence: PresenceModule,
) -> None:
    """After kill_agent writes the metadata marker, is_killed flips True."""
    await presence.heartbeat("agent-1")
    await _kill_agent_in_memory(presence, "agent-1")
    await presence.force_refresh_killed_cache()
    assert await presence.is_killed("agent-1") is True


@pytest.mark.asyncio
async def test_assert_alive_passes_for_live_agent(
    presence: PresenceModule,
) -> None:
    """assert_alive() does not raise for a healthy agent."""
    await presence.heartbeat("agent-1")
    await presence.assert_alive("agent-1")  # must not raise


@pytest.mark.asyncio
async def test_assert_alive_raises_for_killed_agent(
    presence: PresenceModule,
) -> None:
    """assert_alive() raises AgentKilledError with full metadata."""
    await presence.heartbeat("agent-1")
    await _kill_agent_in_memory(
        presence,
        "agent-1",
        killed_by="leo",
        reason="security-test",
        killed_at="2026-04-14T22:00:00+00:00",
    )
    await presence.force_refresh_killed_cache()
    with pytest.raises(AgentKilledError) as exc_info:
        await presence.assert_alive("agent-1")
    err = exc_info.value
    assert err.agent_id == "agent-1"
    assert err.killed_by == "leo"
    assert err.reason == "security-test"
    assert err.killed_at == "2026-04-14T22:00:00+00:00"


@pytest.mark.asyncio
async def test_is_killed_unknown_agent_returns_false(
    presence: PresenceModule,
) -> None:
    """An agent not in the presence table is treated as not-killed."""
    # Edge case: kill switch only knows about agents that ever heartbeated.
    # An agent_id that has no presence row passes through. This is
    # documented behaviour — the kill switch cannot kill what it doesn't
    # know about. Scope.check would still fail with PolicyNotRegistered
    # if no policy was registered.
    assert await presence.is_killed("never-existed") is False


@pytest.mark.asyncio
async def test_assert_alive_unknown_agent_passes(
    presence: PresenceModule,
) -> None:
    """assert_alive on an unknown agent does not raise."""
    await presence.assert_alive("never-existed")  # must not raise


# ---------------------------------------------------------------------------
# 3. Cache TTL behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_warm_no_refresh_within_ttl(
    presence: PresenceModule,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once cache is populated, re-reading within TTL does not re-derive."""
    await presence.heartbeat("agent-1")
    await _kill_agent_in_memory(presence, "agent-1")
    await presence.force_refresh_killed_cache()
    assert await presence.is_killed("agent-1") is True

    # Now mutate the in-memory store to "un-kill" without forcing a refresh.
    # If the cache is honoured, is_killed should still return True.
    async with presence._lock:
        presence._agents["agent-1"]["metadata"].pop("_killed_by")
    assert await presence.is_killed("agent-1") is True  # still cached


@pytest.mark.asyncio
async def test_cache_refresh_after_ttl_expiry(
    presence: PresenceModule,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After TTL expires, the next is_killed() refreshes from the source."""
    await presence.heartbeat("agent-1")
    await _kill_agent_in_memory(presence, "agent-1")
    await presence.force_refresh_killed_cache()
    assert await presence.is_killed("agent-1") is True

    # Un-kill in the in-memory store
    async with presence._lock:
        presence._agents["agent-1"]["metadata"].pop("_killed_by")

    # Fast-forward time past TTL by patching monotonic
    base = time.monotonic()
    monkeypatch.setattr(
        presence_module.time,
        "monotonic",
        lambda: base + presence_module._KILL_CACHE_TTL_SECONDS + 0.1,
    )

    assert await presence.is_killed("agent-1") is False  # refreshed and cleared


@pytest.mark.asyncio
async def test_force_refresh_bypasses_ttl(
    presence: PresenceModule,
) -> None:
    """force_refresh_killed_cache() refreshes immediately even within TTL."""
    await presence.heartbeat("agent-1")
    await presence.force_refresh_killed_cache()  # warm + empty
    assert await presence.is_killed("agent-1") is False

    await _kill_agent_in_memory(presence, "agent-1")
    await presence.force_refresh_killed_cache()  # bypass TTL
    assert await presence.is_killed("agent-1") is True


@pytest.mark.asyncio
async def test_concurrent_refreshes_only_trigger_one_query(
    presence: PresenceModule,
) -> None:
    """Many parallel is_killed() calls share a single refresh cycle.

    Tests the double-checked-locking pattern in _maybe_refresh_killed_cache.
    Counts how many times _derive_killed_from_memory is actually invoked
    when 50 coroutines call is_killed() simultaneously.
    """
    await presence.heartbeat("agent-1")

    call_count = 0
    original = presence._derive_killed_from_memory

    def counting_derive() -> dict[str, dict]:
        nonlocal call_count
        call_count += 1
        return original()

    presence._derive_killed_from_memory = counting_derive  # type: ignore[method-assign]

    # Force the first refresh to be needed (cache is "stale" because never set)
    presence._killed_cache_at = 0.0

    # Fire 50 concurrent is_killed calls
    results = await asyncio.gather(
        *[presence.is_killed("agent-1") for _ in range(50)]
    )
    assert all(r is False for r in results)
    # The double-checked lock should keep this at 1, but a tolerant assertion
    # accepts up to 2 (race window between the outer check and the lock acquire).
    assert call_count <= 2, f"expected ≤2 derive calls, got {call_count}"


# ---------------------------------------------------------------------------
# 4. Invariant #1 — DB outage resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_error_during_refresh_keeps_existing_cache(
    presence: PresenceModule,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the DB query raises, the existing cache is preserved (Invariant #1).

    Simulates an engine that throws on connect. The cache should NOT clear,
    no exception should propagate, and a warning is logged.
    """
    # Seed the cache with a known killed agent
    presence._killed_cache = {
        "killed-1": {"killed_by": "old-op", "killed_at": "x", "reason": "y"}
    }
    presence._killed_cache_at = time.monotonic()

    # Inject a fake engine that raises on connect()
    class _BoomEngine:
        def connect(self) -> None:
            raise RuntimeError("simulated DB outage")

    presence._engine = _BoomEngine()

    # Force a refresh
    presence._killed_cache_at = 0.0
    # Must not raise even though the DB is "down"
    await presence._maybe_refresh_killed_cache()

    # The cache must still hold the old entry
    assert "killed-1" in presence._killed_cache
    # And is_killed must still return True for the cached entry
    assert await presence.is_killed("killed-1") is True


@pytest.mark.asyncio
async def test_db_error_bumps_cache_timestamp(
    presence: PresenceModule,
) -> None:
    """A failed refresh bumps the timestamp so we don't hammer the DB.

    Without this, every subsequent is_killed() call during an outage would
    re-attempt the failing query.
    """
    class _BoomEngine:
        def connect(self) -> None:
            raise RuntimeError("simulated DB outage")

    presence._engine = _BoomEngine()
    presence._killed_cache_at = 0.0

    before = time.monotonic()
    await presence._maybe_refresh_killed_cache()
    after_first = presence._killed_cache_at

    # The bumped timestamp should be >= before (best-effort recent)
    assert after_first >= before

    # Immediate next call should NOT trigger another connect (within TTL)
    connect_attempts = 0
    original_engine = presence._engine

    class _CountingEngine:
        def connect(self) -> None:
            nonlocal connect_attempts
            connect_attempts += 1
            raise RuntimeError("should not be called")

    presence._engine = _CountingEngine()
    await presence._maybe_refresh_killed_cache()
    assert connect_attempts == 0


@pytest.mark.asyncio
async def test_assert_alive_never_raises_db_errors(
    presence: PresenceModule,
) -> None:
    """assert_alive() surfaces ONLY AgentKilledError, never DB errors."""
    class _BoomEngine:
        def connect(self) -> None:
            raise RuntimeError("simulated DB outage")

    presence._engine = _BoomEngine()
    presence._killed_cache_at = 0.0

    # No cached killed agents + DB down → assert_alive must pass cleanly
    await presence.assert_alive("agent-1")  # must not raise


# ---------------------------------------------------------------------------
# 5. In-memory mode (no engine)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_memory_mode_derives_from_agents_dict(
    presence: PresenceModule,
) -> None:
    """With no engine configured, kill state derives from _agents metadata."""
    assert presence._engine is None  # default fixture has no engine

    await presence.heartbeat("agent-a", metadata={"region": "us"})
    await presence.heartbeat("agent-b", metadata={"region": "eu"})
    await _kill_agent_in_memory(
        presence, "agent-b", killed_by="ops", reason="rogue"
    )
    await presence.force_refresh_killed_cache()

    assert await presence.is_killed("agent-a") is False
    assert await presence.is_killed("agent-b") is True


@pytest.mark.asyncio
async def test_in_memory_kill_metadata_round_trip(
    presence: PresenceModule,
) -> None:
    """assert_alive() in memory mode surfaces all 3 kill metadata fields."""
    await presence.heartbeat("agent-1")
    await _kill_agent_in_memory(
        presence,
        "agent-1",
        killed_by="op-2",
        killed_at="2026-04-14T23:00:00+00:00",
        reason="prevent-bypass",
    )
    await presence.force_refresh_killed_cache()
    with pytest.raises(AgentKilledError) as exc_info:
        await presence.assert_alive("agent-1")
    err = exc_info.value
    assert err.killed_by == "op-2"
    assert err.killed_at == "2026-04-14T23:00:00+00:00"
    assert err.reason == "prevent-bypass"


# ---------------------------------------------------------------------------
# 6. Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_without_killed_marker_not_killed(
    presence: PresenceModule,
) -> None:
    """Metadata with other fields but no _killed_by is NOT killed."""
    await presence.heartbeat(
        "agent-1",
        metadata={"version": "1.0", "region": "us-east"},
    )
    await presence.force_refresh_killed_cache()
    assert await presence.is_killed("agent-1") is False


@pytest.mark.asyncio
async def test_kill_with_partial_metadata_still_kills(
    presence: PresenceModule,
) -> None:
    """Even if killed_at + reason are missing, _killed_by alone triggers kill."""
    await presence.heartbeat("agent-1")
    async with presence._lock:
        presence._agents["agent-1"].setdefault("metadata", {})["_killed_by"] = "leo"
    await presence.force_refresh_killed_cache()
    assert await presence.is_killed("agent-1") is True
    with pytest.raises(AgentKilledError) as exc_info:
        await presence.assert_alive("agent-1")
    assert exc_info.value.killed_by == "leo"
    assert exc_info.value.killed_at is None
    assert exc_info.value.reason is None


@pytest.mark.asyncio
async def test_many_killed_agents_cache_scales(
    presence: PresenceModule,
) -> None:
    """Cache holds N killed agents without per-agent overhead."""
    for i in range(500):
        await presence.heartbeat(f"agent-{i}")
        if i % 2 == 0:  # kill every even agent
            await _kill_agent_in_memory(presence, f"agent-{i}")
    await presence.force_refresh_killed_cache()
    assert len(presence._killed_cache) == 250
    assert await presence.is_killed("agent-0") is True
    assert await presence.is_killed("agent-1") is False
    assert await presence.is_killed("agent-498") is True
    assert await presence.is_killed("agent-499") is False


@pytest.mark.asyncio
async def test_unicode_agent_id_killed(
    presence: PresenceModule,
) -> None:
    """Kill switch handles non-ASCII agent IDs."""
    aid = "agent-ü-日本-🤖"
    await presence.heartbeat(aid)
    await _kill_agent_in_memory(presence, aid)
    await presence.force_refresh_killed_cache()
    assert await presence.is_killed(aid) is True


@pytest.mark.asyncio
async def test_un_kill_via_metadata_removal(
    presence: PresenceModule,
) -> None:
    """Removing _killed_by + force_refresh restores the agent."""
    await presence.heartbeat("agent-1")
    await _kill_agent_in_memory(presence, "agent-1")
    await presence.force_refresh_killed_cache()
    assert await presence.is_killed("agent-1") is True

    # SQL-equivalent un-kill: drop the marker
    async with presence._lock:
        meta = presence._agents["agent-1"]["metadata"]
        for k in ("_killed_by", "_killed_at", "_kill_reason"):
            meta.pop(k, None)

    await presence.force_refresh_killed_cache()
    assert await presence.is_killed("agent-1") is False
    await presence.assert_alive("agent-1")  # must not raise


@pytest.mark.asyncio
async def test_re_kill_idempotent(
    presence: PresenceModule,
) -> None:
    """Killing an already-killed agent is a no-op (idempotent)."""
    await presence.heartbeat("agent-1")
    await _kill_agent_in_memory(presence, "agent-1", killed_by="op-1")
    await presence.force_refresh_killed_cache()
    assert await presence.is_killed("agent-1") is True

    # Second kill with different operator overwrites the metadata
    await _kill_agent_in_memory(presence, "agent-1", killed_by="op-2", reason="round 2")
    await presence.force_refresh_killed_cache()
    with pytest.raises(AgentKilledError) as exc_info:
        await presence.assert_alive("agent-1")
    assert exc_info.value.killed_by == "op-2"
    assert exc_info.value.reason == "round 2"


# ---------------------------------------------------------------------------
# 7. ScopeModule integration (the real enforcement test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_check_passes_when_no_presence_module() -> None:
    """ScopeModule with no presence wired = back-compat (no kill check).

    Verifies the back-compat path: SDK constructed with enable_presence=False
    does NOT crash scope.check() — it skips the kill check silently.
    """
    from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
    from codeatelier_governance.scope.module import ScopeModule
    from codeatelier_governance.scope.models import ScopePolicy

    import secrets as _secrets
    audit = AuditModule(InMemoryAuditStore(max_events=1000), secret=_secrets.token_bytes(32))
    scope = ScopeModule(audit)
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"read"})))
    assert scope._presence is None
    await scope.check("a", tool="read")  # must not raise


@pytest.mark.asyncio
async def test_scope_check_blocks_killed_agent(
    presence: PresenceModule,
) -> None:
    """ScopeModule with presence wired raises AgentKilledError on killed agent."""
    from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
    from codeatelier_governance.scope.module import ScopeModule
    from codeatelier_governance.scope.models import ScopePolicy

    import secrets as _secrets
    audit = AuditModule(InMemoryAuditStore(max_events=1000), secret=_secrets.token_bytes(32))
    scope = ScopeModule(audit)
    scope.set_presence_module(presence)
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"read"})))

    # Phase 1: agent is alive
    await presence.heartbeat("a")
    await scope.check("a", tool="read")

    # Phase 2: kill via metadata marker (simulates console kill_agent)
    await _kill_agent_in_memory(presence, "a", killed_by="leo", reason="test")
    await presence.force_refresh_killed_cache()

    # Phase 3: scope check now fails closed
    with pytest.raises(AgentKilledError) as exc_info:
        await scope.check("a", tool="read")
    assert exc_info.value.agent_id == "a"
    assert exc_info.value.killed_by == "leo"


@pytest.mark.asyncio
async def test_scope_check_kill_check_runs_before_policy_lookup(
    presence: PresenceModule,
) -> None:
    """A killed agent fails kill check even with no scope policy registered.

    Proves the ordering: kill check fires BEFORE PolicyNotRegistered.
    Without this ordering, an attacker could un-register a policy to bypass
    the kill check (no actual exploit path today, but ordering matters).
    """
    from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
    from codeatelier_governance.scope.module import ScopeModule

    import secrets as _secrets
    audit = AuditModule(InMemoryAuditStore(max_events=1000), secret=_secrets.token_bytes(32))
    scope = ScopeModule(audit)
    scope.set_presence_module(presence)

    await presence.heartbeat("ghost")
    await _kill_agent_in_memory(presence, "ghost")
    await presence.force_refresh_killed_cache()

    # No policy registered — would normally raise PolicyNotRegistered.
    # But kill check runs FIRST, so we get AgentKilledError instead.
    with pytest.raises(AgentKilledError):
        await scope.check("ghost", tool="anything")


@pytest.mark.asyncio
async def test_scope_check_kill_check_runs_before_value_error(
    presence: PresenceModule,
) -> None:
    """Kill check runs before the tool=None+api=None ValueError."""
    from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
    from codeatelier_governance.scope.module import ScopeModule

    import secrets as _secrets
    audit = AuditModule(InMemoryAuditStore(max_events=1000), secret=_secrets.token_bytes(32))
    scope = ScopeModule(audit)
    scope.set_presence_module(presence)

    await presence.heartbeat("a")
    await _kill_agent_in_memory(presence, "a")
    await presence.force_refresh_killed_cache()

    # Kill check fires before the "pass tool or api" validation.
    with pytest.raises(AgentKilledError):
        await scope.check("a")  # missing tool/api, but agent is killed first
