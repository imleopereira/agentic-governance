"""Tests for the loop / anomaly detection module."""
from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from codeatelier_governance.audit import InMemoryAuditStore
from codeatelier_governance.loop import (
    LoopDetected,
    LoopModule,
    LoopPolicy,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_under_threshold_passes(loop: LoopModule) -> None:
    """Calls below the threshold should not raise."""
    loop.register(LoopPolicy(agent_id="a", max_calls=5, window_seconds=60))
    sid = uuid4()
    for _ in range(5):
        await loop.record_call("a", sid, "read_file")
    # 5 calls = exactly at threshold, should NOT raise (> not >=)


@pytest.mark.asyncio
async def test_at_threshold_raises(loop: LoopModule) -> None:
    """Exceeding max_calls should raise LoopDetected."""
    loop.register(LoopPolicy(agent_id="a", max_calls=3, window_seconds=60))
    sid = uuid4()
    await loop.record_call("a", sid, "read_file")
    await loop.record_call("a", sid, "read_file")
    await loop.record_call("a", sid, "read_file")
    with pytest.raises(LoopDetected, match="loop detected"):
        await loop.record_call("a", sid, "read_file")


@pytest.mark.asyncio
async def test_different_tools_dont_trigger(loop: LoopModule) -> None:
    """Different tool names should be tracked independently."""
    loop.register(LoopPolicy(agent_id="a", max_calls=2, window_seconds=60))
    sid = uuid4()
    await loop.record_call("a", sid, "tool_a")
    await loop.record_call("a", sid, "tool_b")
    await loop.record_call("a", sid, "tool_a")
    # 2 calls to tool_a, 1 to tool_b -- neither exceeds 2


@pytest.mark.asyncio
async def test_window_expiry(loop: LoopModule) -> None:
    """Old calls outside the window should not count."""
    loop.register(LoopPolicy(agent_id="a", max_calls=2, window_seconds=1))
    sid = uuid4()
    await loop.record_call("a", sid, "read_file")
    await loop.record_call("a", sid, "read_file")
    # Wait for window to expire
    await asyncio.sleep(1.1)
    # These should be fine now -- old calls expired
    await loop.record_call("a", sid, "read_file")
    await loop.record_call("a", sid, "read_file")


@pytest.mark.asyncio
async def test_action_log_does_not_raise(
    loop: LoopModule, audit_store: InMemoryAuditStore,
) -> None:
    """action='log' should emit event but not raise."""
    loop.register(
        LoopPolicy(agent_id="a", max_calls=2, window_seconds=60, action="log")
    )
    sid = uuid4()
    await loop.record_call("a", sid, "read_file")
    await loop.record_call("a", sid, "read_file")
    # This should NOT raise even though it exceeds threshold
    await loop.record_call("a", sid, "read_file")
    # Wait for audit flush
    await asyncio.sleep(0.1)
    events = [
        e
        for e in audit_store._events.values()
        if e.kind == "loop.detected"
    ]
    assert len(events) >= 1
    assert events[0].metadata["tool_name"] == "read_file"


@pytest.mark.asyncio
async def test_audit_event_emitted_on_detection(
    loop: LoopModule, audit_store: InMemoryAuditStore,
) -> None:
    """Loop detection should emit a loop.detected audit event."""
    loop.register(LoopPolicy(agent_id="a", max_calls=2, window_seconds=60))
    sid = uuid4()
    await loop.record_call("a", sid, "read_file")
    await loop.record_call("a", sid, "read_file")
    with pytest.raises(LoopDetected):
        await loop.record_call("a", sid, "read_file")
    await asyncio.sleep(0.1)
    events = [
        e
        for e in audit_store._events.values()
        if e.kind == "loop.detected"
    ]
    assert len(events) == 1
    assert events[0].metadata["tool_name"] == "read_file"
    assert events[0].metadata["max_calls"] == 2
    assert events[0].session_id == sid


@pytest.mark.asyncio
async def test_policy_registration(loop: LoopModule) -> None:
    """register() should make the policy active."""
    policy = LoopPolicy(agent_id="test-agent", max_calls=10, window_seconds=120)
    loop.register(policy)
    assert loop._policies["test-agent"] is policy


@pytest.mark.asyncio
async def test_no_policy_is_noop(loop: LoopModule) -> None:
    """Without a registered policy, record_call should be a no-op."""
    sid = uuid4()
    # 100 calls with no policy -- should never raise
    for _ in range(100):
        await loop.record_call("unregistered", sid, "read_file")


@pytest.mark.asyncio
async def test_cleanup_of_old_entries(loop: LoopModule) -> None:
    """Old entries (>24h) should be cleaned up opportunistically."""
    loop.register(LoopPolicy(agent_id="a", max_calls=100, window_seconds=60))
    sid = uuid4()
    sid_old = uuid4()
    # Insert some "old" entries by manipulating internal state
    old_time = time.time() - 90000  # > 24h ago
    loop._calls[("a", sid_old)] = [("tool_x", old_time)]

    # Record a new call which triggers cleanup
    await loop.record_call("a", sid, "tool_y")

    # Old session should be cleaned up
    assert ("a", sid_old) not in loop._calls


@pytest.mark.asyncio
async def test_check_readonly_detects_loop(loop: LoopModule) -> None:
    """check() should detect loops without recording a new call."""
    loop.register(LoopPolicy(agent_id="a", max_calls=2, window_seconds=60))
    sid = uuid4()
    await loop.record_call("a", sid, "read_file")
    await loop.record_call("a", sid, "read_file")
    # check should not raise yet -- exactly at threshold
    await loop.check("a", sid)
    # Add one more to exceed
    with pytest.raises(LoopDetected):
        await loop.record_call("a", sid, "read_file")
    # Now check should also detect the loop
    with pytest.raises(LoopDetected):
        await loop.check("a", sid)


@pytest.mark.asyncio
async def test_different_sessions_independent(loop: LoopModule) -> None:
    """Different sessions should be tracked independently."""
    loop.register(LoopPolicy(agent_id="a", max_calls=2, window_seconds=60))
    sid1 = uuid4()
    sid2 = uuid4()
    await loop.record_call("a", sid1, "read_file")
    await loop.record_call("a", sid1, "read_file")
    # sid2 should be fine
    await loop.record_call("a", sid2, "read_file")
    await loop.record_call("a", sid2, "read_file")


@pytest.mark.asyncio
async def test_varying_input_does_not_bypass(loop: LoopModule) -> None:
    """Detection key is (session_id, tool_name) -- varying input should not bypass."""
    loop.register(LoopPolicy(agent_id="a", max_calls=2, window_seconds=60))
    sid = uuid4()
    # Same tool name even if input varies
    await loop.record_call("a", sid, "read_file")
    await loop.record_call("a", sid, "read_file")
    with pytest.raises(LoopDetected):
        await loop.record_call("a", sid, "read_file")
