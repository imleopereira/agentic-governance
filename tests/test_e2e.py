"""End-to-end "5 lines to enforcement" test.

This is the test that goes in the README. It proves the pitch:
init SDK modules, register scope, check tool, log event, verify chain.
All in one test, under 10 lines of test body.
"""
from __future__ import annotations

import secrets

import pytest

from codeatelier_governance.audit import (
    AuditEvent,
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.scope import ScopeModule, ScopePolicy, ScopeViolation


@pytest.mark.asyncio
async def test_five_lines_to_enforcement() -> None:
    """Init SDK, register scope, check tool, log event, verify chain."""
    store = InMemoryAuditStore()
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=BatchingWriter(primary=store, batch_size=1, flush_interval_s=0.01))
    await audit.start()
    scope = ScopeModule(audit)
    scope.register(ScopePolicy(agent_id="bot", allowed_tools=frozenset({"read"})))
    with pytest.raises(ScopeViolation):
        await scope.check(agent_id="bot", tool="delete")       # blocked
    record = await audit.log(AuditEvent(agent_id="bot", kind="tool.call"))
    await audit._writer.flush()
    chain = await audit.trace_session_chain(record.session_id)  # verified
    assert len(chain) >= 1
    await audit.close()
