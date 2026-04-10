"""Tests for the agent presence module."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from codeatelier_governance.presence import AgentStatus, PresenceModule


@pytest.mark.asyncio
async def test_heartbeat_creates_live_agent(presence: PresenceModule) -> None:
    """heartbeat() should create an agent with 'live' status."""
    await presence.heartbeat("agent-1")
    agents = await presence.list_agents()
    assert len(agents) == 1
    assert agents[0]["agent_id"] == "agent-1"
    assert agents[0]["status"] == "live"


@pytest.mark.asyncio
async def test_heartbeat_with_metadata(presence: PresenceModule) -> None:
    """heartbeat() should store metadata."""
    await presence.heartbeat("agent-1", metadata={"version": "1.0"})
    agents = await presence.list_agents()
    assert agents[0]["metadata"] == {"version": "1.0"}


@pytest.mark.asyncio
async def test_mark_idle(presence: PresenceModule) -> None:
    """mark_idle() should set status to 'idle'."""
    await presence.heartbeat("agent-1")
    await presence.mark_idle("agent-1")
    agents = await presence.list_agents()
    assert agents[0]["status"] == "idle"


@pytest.mark.asyncio
async def test_close_agent_removes(presence: PresenceModule) -> None:
    """close_agent() should remove the agent from the list."""
    await presence.heartbeat("agent-1")
    await presence.close_agent("agent-1")
    agents = await presence.list_agents()
    assert len(agents) == 0


@pytest.mark.asyncio
async def test_stale_detection(presence: PresenceModule) -> None:
    """check_stale() should mark agents with old heartbeats as unresponsive."""
    await presence.heartbeat("agent-1")
    # Backdate the heartbeat
    async with presence._lock:
        presence._agents["agent-1"]["last_heartbeat"] = (
            datetime.now(timezone.utc) - timedelta(seconds=600)
        )
    await presence.check_stale(timeout_seconds=300)
    agents = await presence.list_agents()
    assert agents[0]["status"] == "unresponsive"


@pytest.mark.asyncio
async def test_stale_does_not_affect_fresh(presence: PresenceModule) -> None:
    """check_stale() should not affect freshly heartbeated agents."""
    await presence.heartbeat("agent-1")
    await presence.check_stale(timeout_seconds=300)
    agents = await presence.list_agents()
    assert agents[0]["status"] == "live"


@pytest.mark.asyncio
async def test_list_multiple_agents(presence: PresenceModule) -> None:
    """list_agents() should return all agents."""
    await presence.heartbeat("agent-1")
    await presence.heartbeat("agent-2")
    await presence.mark_idle("agent-2")
    agents = await presence.list_agents()
    assert len(agents) == 2
    by_id = {a["agent_id"]: a for a in agents}
    assert by_id["agent-1"]["status"] == "live"
    assert by_id["agent-2"]["status"] == "idle"


@pytest.mark.asyncio
async def test_heartbeat_updates_existing(presence: PresenceModule) -> None:
    """Subsequent heartbeat should update status back to live."""
    await presence.heartbeat("agent-1")
    await presence.mark_idle("agent-1")
    await presence.heartbeat("agent-1")
    agents = await presence.list_agents()
    assert agents[0]["status"] == "live"


@pytest.mark.asyncio
async def test_close_nonexistent_is_noop(presence: PresenceModule) -> None:
    """close_agent() on a nonexistent agent should not raise."""
    await presence.close_agent("nonexistent")


@pytest.mark.asyncio
async def test_agent_status_enum() -> None:
    """AgentStatus enum should have the expected values."""
    assert AgentStatus.LIVE == "live"
    assert AgentStatus.IDLE == "idle"
    assert AgentStatus.UNRESPONSIVE == "unresponsive"
