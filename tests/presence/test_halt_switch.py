"""v0.6 F2.5 halt switch — canonical (post-rename) test suite.

Mirrors the v0.5.4 ``test_kill_switch.py`` suite but uses the v0.6 ``halt``
vocabulary: ``is_halted``, ``assert_not_halted``, ``AgentHaltedError``,
``_halted_cache``, ``_halted_cache_at``, ``force_refresh_halted_cache``,
and the ``_halted_by`` / ``_halted_at`` / ``_halt_reason`` presence
metadata keys.

``test_kill_switch.py`` stays in the tree and exercises the v0.5.x
back-compat aliases against the same module. This file is the forward
contract.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from codeatelier_governance.presence import (
    AgentHaltedError,
    AgentStatus,
    PresenceModule,
)
from codeatelier_governance.presence import module as presence_module


async def _halt_agent_in_memory(
    presence: PresenceModule,
    agent_id: str,
    *,
    halted_by: str = "test-operator",
    reason: str = "test",
    halted_at: str | None = None,
) -> None:
    """Simulate the console halt_agent endpoint in the in-memory store."""
    if halted_at is None:
        halted_at = datetime.now(timezone.utc).isoformat()
    async with presence._lock:
        if agent_id not in presence._agents:
            raise RuntimeError(
                f"agent {agent_id!r} not in presence; call heartbeat() first"
            )
        meta = presence._agents[agent_id].setdefault("metadata", {})
        meta["_halted_by"] = halted_by
        meta["_halted_at"] = halted_at
        meta["_halt_reason"] = reason
        presence._agents[agent_id]["status"] = AgentStatus.UNRESPONSIVE.value


# ---------------------------------------------------------------------------
# 1. AgentHaltedError construction
# ---------------------------------------------------------------------------


def test_agent_halted_error_minimal() -> None:
    err = AgentHaltedError("agent-x")
    assert err.agent_id == "agent-x"
    assert err.halted_by is None
    assert err.halted_at is None
    assert err.reason is None
    assert "agent-x" in str(err)
    assert "fail-closed" in str(err)


def test_agent_halted_error_full_metadata() -> None:
    err = AgentHaltedError(
        "agent-y",
        halted_by="leo",
        halted_at="2026-04-14T22:00:00",
        reason="incident-001",
    )
    s = str(err)
    assert "agent-y" in s
    assert "leo" in s
    assert "incident-001" in s


def test_agent_halted_error_is_runtime_error() -> None:
    err = AgentHaltedError("a")
    assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# 2. Happy path — alive vs halted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_halted_returns_false_for_live_agent(
    presence: PresenceModule,
) -> None:
    await presence.heartbeat("agent-1")
    assert await presence.is_halted("agent-1") is False


@pytest.mark.asyncio
async def test_is_halted_returns_true_after_halt(
    presence: PresenceModule,
) -> None:
    await presence.heartbeat("agent-1")
    await _halt_agent_in_memory(presence, "agent-1")
    await presence.force_refresh_halted_cache()
    assert await presence.is_halted("agent-1") is True


@pytest.mark.asyncio
async def test_assert_not_halted_passes_for_live_agent(
    presence: PresenceModule,
) -> None:
    await presence.heartbeat("agent-1")
    await presence.assert_not_halted("agent-1")  # must not raise


@pytest.mark.asyncio
async def test_assert_not_halted_raises_for_halted_agent(
    presence: PresenceModule,
) -> None:
    await presence.heartbeat("agent-1")
    await _halt_agent_in_memory(
        presence,
        "agent-1",
        halted_by="leo",
        reason="security-test",
        halted_at="2026-04-14T22:00:00+00:00",
    )
    await presence.force_refresh_halted_cache()
    with pytest.raises(AgentHaltedError) as exc_info:
        await presence.assert_not_halted("agent-1")
    err = exc_info.value
    assert err.agent_id == "agent-1"
    assert err.halted_by == "leo"
    assert err.reason == "security-test"
    assert err.halted_at == "2026-04-14T22:00:00+00:00"


# ---------------------------------------------------------------------------
# 3. v0.5.x back-compat: presence module reads `_killed_*` keys too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_killed_metadata_still_halts_agent(
    presence: PresenceModule,
) -> None:
    """A presence row written by v0.5.4 with `_killed_*` keys must still
    fail closed under v0.6. This is the upgrade-without-console-upgrade
    case — the SDK must read BOTH key families for one release."""
    await presence.heartbeat("agent-1")
    async with presence._lock:
        meta = presence._agents["agent-1"].setdefault("metadata", {})
        meta["_killed_by"] = "legacy-op"
        meta["_killed_at"] = "2026-04-14T00:00:00+00:00"
        meta["_kill_reason"] = "v0.5.4 marker"
    await presence.force_refresh_halted_cache()
    assert await presence.is_halted("agent-1") is True
    with pytest.raises(AgentHaltedError) as exc_info:
        await presence.assert_not_halted("agent-1")
    assert exc_info.value.halted_by == "legacy-op"
    assert exc_info.value.reason == "v0.5.4 marker"


# ---------------------------------------------------------------------------
# 4. Cache + TTL wiring under the halt name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_refresh_halted_cache_bypasses_ttl(
    presence: PresenceModule,
) -> None:
    await presence.heartbeat("agent-1")
    await presence.force_refresh_halted_cache()
    assert await presence.is_halted("agent-1") is False
    await _halt_agent_in_memory(presence, "agent-1")
    await presence.force_refresh_halted_cache()
    assert await presence.is_halted("agent-1") is True


@pytest.mark.asyncio
async def test_halt_cache_ttl_constant_is_exposed() -> None:
    """The module-level TTL constant exists under the new name and matches
    the v0.5.x back-compat alias."""
    assert presence_module._HALT_CACHE_TTL_SECONDS == 5.0
    assert (
        presence_module._KILL_CACHE_TTL_SECONDS
        == presence_module._HALT_CACHE_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_concurrent_halted_refreshes_only_derive_once(
    presence: PresenceModule,
) -> None:
    await presence.heartbeat("agent-1")
    call_count = 0
    original = presence._derive_halted_from_memory

    def counting() -> dict[str, dict]:
        nonlocal call_count
        call_count += 1
        return original()

    presence._derive_halted_from_memory = counting  # type: ignore[method-assign]
    presence._halted_cache_at = 0.0

    results = await asyncio.gather(
        *[presence.is_halted("agent-1") for _ in range(50)]
    )
    assert all(r is False for r in results)
    assert call_count <= 2
