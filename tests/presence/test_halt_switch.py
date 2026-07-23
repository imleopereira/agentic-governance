"""v0.6 F2.5 halt switch — canonical (post-rename) test suite.

Mirrors the v0.5.4 ``test_kill_switch.py`` suite but uses the v0.6 ``halt``
vocabulary: ``is_halted``, ``assert_not_halted``, ``AgentHaltedError``,
``_halted_cache``, ``_halted_cache_at``, ``force_refresh_halted_cache``,
and the dedicated ``halted_by`` / ``halted_at`` / ``halt_reason`` presence
columns (the in-memory analogue is the matching top-level marker keys).

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
    """Halt an agent in the in-memory store via the dedicated marker.

    Sets the top-level ``halted_by`` / ``halted_at`` / ``halt_reason`` keys
    — the in-memory analogue of the revoke-protected DB columns written by
    :meth:`PresenceModule.halt`. (Through v0.6 an operator halt was written
    into ``metadata_json``; v0.7 moved it to dedicated columns because a
    metadata marker was self-clearable by the agent's own heartbeat.)
    """
    if halted_at is None:
        halted_at = datetime.now(timezone.utc).isoformat()
    async with presence._lock:
        if agent_id not in presence._agents:
            raise RuntimeError(
                f"agent {agent_id!r} not in presence; call heartbeat() first"
            )
        presence._agents[agent_id]["halted_by"] = halted_by
        presence._agents[agent_id]["halted_at"] = halted_at
        presence._agents[agent_id]["halt_reason"] = reason
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
# 3. v0.7: legacy `_killed_*` / `_halted_*` metadata markers are NOT honored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_metadata_marker_does_not_halt_agent(
    presence: PresenceModule,
) -> None:
    """v0.7 reads the halt from the dedicated columns only, never metadata.

    A halt marker stored in ``metadata_json`` (the pre-v0.7 representation)
    is self-clearable by the agent's own heartbeat, so v0.7 stopped honoring
    it. Migration a7f2haltcols backfilled any real pre-v0.7 markers into the
    columns, so a row carrying ONLY the legacy metadata keys must now read as
    NOT halted — otherwise the agent could re-open the self-unhalt vector by
    writing the keys back into its own metadata."""
    await presence.heartbeat("agent-1")
    async with presence._lock:
        meta = presence._agents["agent-1"].setdefault("metadata", {})
        meta["_killed_by"] = "legacy-op"
        meta["_killed_at"] = "2026-04-14T00:00:00+00:00"
        meta["_kill_reason"] = "v0.5.4 marker"
    await presence.force_refresh_halted_cache()
    assert await presence.is_halted("agent-1") is False
    await presence.assert_not_halted("agent-1")  # must not raise


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
