"""Regression: a heartbeat must never clear an operator halt (self-unhalt).

SECURITY FIX (self-unhalt-by-heartbeat).

Through v0.6 the halt marker lived inside ``metadata_json`` (memory:
``_agents[aid]['metadata']``), which the heartbeat rewrites wholesale on
every beat. A halted agent that kept heartbeating erased its own halt on
the next beat. The fix moves the marker into dedicated storage that the
heartbeat path never writes — DB columns halted_by/halted_at/halt_reason
(revoke-protected), and top-level keys in the in-memory store carried
forward across heartbeats exactly like ``started_at``.

These tests run against the in-memory :class:`PresenceModule` (the
``presence`` fixture in conftest.py — ``PresenceModule()`` with no
engine), so they need no live Postgres. On the OLD design the first test
fails: the second heartbeat would drop the marker and ``is_halted`` would
flip back to False.
"""
from __future__ import annotations

import pytest

from codeatelier_governance.presence import AgentHaltedError, PresenceModule


@pytest.mark.asyncio
async def test_heartbeat_after_halt_preserves_halt(
    presence: PresenceModule,
) -> None:
    """Halt an agent, then let it heartbeat: it must stay halted."""
    await presence.heartbeat("agent-1")

    await presence.halt("agent-1", halted_by="leo", reason="incident")
    await presence.force_refresh_halted_cache()
    assert await presence.is_halted("agent-1") is True

    # The self-unhalt attempt: the agent keeps beating, this time with
    # fresh metadata. On the old design this wholesale metadata rewrite
    # erased the halt marker.
    await presence.heartbeat("agent-1", metadata={"foo": "bar"})
    await presence.force_refresh_halted_cache()

    assert await presence.is_halted("agent-1") is True
    with pytest.raises(AgentHaltedError) as exc_info:
        await presence.assert_not_halted("agent-1")
    assert exc_info.value.agent_id == "agent-1"
    assert exc_info.value.halted_by == "leo"
    assert exc_info.value.reason == "incident"


@pytest.mark.asyncio
async def test_heartbeat_after_halt_still_applies_new_metadata(
    presence: PresenceModule,
) -> None:
    """The heartbeat must still update metadata while preserving the halt.

    Proves the fix does not simply freeze the row: the heartbeat write
    path keeps working (metadata foo=bar becomes visible), it just cannot
    touch the halt marker.
    """
    await presence.heartbeat("agent-1")
    await presence.halt("agent-1", halted_by="leo", reason="incident")

    await presence.heartbeat("agent-1", metadata={"foo": "bar"})

    agents = {a["agent_id"]: a for a in await presence.list_agents()}
    assert agents["agent-1"]["metadata"] == {"foo": "bar"}

    await presence.force_refresh_halted_cache()
    assert await presence.is_halted("agent-1") is True


@pytest.mark.asyncio
async def test_close_agent_after_halt_cannot_clear_halt(
    presence: PresenceModule,
) -> None:
    """A halted agent must not clear its own halt via close_agent + heartbeat.

    close_agent() removes the presence row and a following heartbeat
    re-creates it. On the first fix (which only guarded the heartbeat UPDATE)
    the recreated row came back unhalted. The halt must survive this second
    write path too: close_agent refuses to remove a halted agent in memory,
    and a BEFORE DELETE trigger blocks the DELETE in Postgres.
    """
    await presence.heartbeat("agent-1")
    await presence.halt("agent-1", halted_by="leo", reason="incident")
    await presence.force_refresh_halted_cache()
    assert await presence.is_halted("agent-1") is True

    # The self-unhalt attempt via a different write path: close, then beat.
    await presence.close_agent("agent-1")
    await presence.heartbeat("agent-1", metadata={"foo": "bar"})
    await presence.force_refresh_halted_cache()

    assert await presence.is_halted("agent-1") is True
    with pytest.raises(AgentHaltedError) as exc_info:
        await presence.assert_not_halted("agent-1")
    assert exc_info.value.halted_by == "leo"
