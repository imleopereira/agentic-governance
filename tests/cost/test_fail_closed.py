"""Tests for the fail-closed semantics on cost.check_or_raise()."""
from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.cost import (
    BudgetExceeded,
    BudgetPolicy,
    CostModule,
)
from codeatelier_governance.cost.store import CostStore


class _BrokenCostStore(CostStore):
    """A cost store whose reads always fail. Models DB unreachable."""

    async def track(self, agent_id, session_id, *, tokens, usd):  # type: ignore[no-untyped-def]
        return None  # writes work, reads fail

    async def get_session_usage(self, agent_id, session_id):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated cost store unreachable")

    async def get_agent_daily_usage(self, agent_id):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated cost store unreachable")


@pytest.fixture
async def audit():  # type: ignore[no-untyped-def]
    store = InMemoryAuditStore()
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02)
    module = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    await module.start()
    try:
        yield module
    finally:
        await module.close()


@pytest.mark.asyncio
async def test_fail_closed_default_raises_when_store_down(audit) -> None:  # type: ignore[no-untyped-def]
    """Default fail_open=False — storage failure → BudgetExceeded raised."""
    cost = CostModule(audit, store=_BrokenCostStore())
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=1.0))
    with pytest.raises(BudgetExceeded, match="failing closed"):
        await cost.check_or_raise("a", uuid4())


@pytest.mark.asyncio
async def test_fail_open_explicit_allows_when_store_down(audit) -> None:  # type: ignore[no-untyped-def]
    """fail_open=True → storage failure logs and allows the call."""
    cost = CostModule(audit, store=_BrokenCostStore(), fail_open=True)
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=1.0))
    # Must NOT raise — fail_open opted in
    await cost.check_or_raise("a", uuid4())


@pytest.mark.asyncio
async def test_fail_closed_writes_audit_event(
    audit, audit_store=None  # type: ignore[no-untyped-def]
) -> None:
    """Storage failure must produce a budget.check_failed audit event."""
    inner_store = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=inner_store, batch_size=5, flush_interval_s=0.02
    )
    audit_mod = AuditModule(
        inner_store, secret=secrets.token_bytes(32), writer=writer
    )
    await audit_mod.start()
    try:
        cost = CostModule(audit_mod, store=_BrokenCostStore())
        cost.register(BudgetPolicy(agent_id="a", per_session_usd=1.0))
        with pytest.raises(BudgetExceeded):
            await cost.check_or_raise("a", uuid4())
    finally:
        await audit_mod.close()
    events = [
        e
        for e in inner_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "budget.check_failed"
    ]
    assert len(events) == 1
    assert events[0].metadata["fail_mode"] == "closed"


@pytest.mark.asyncio
async def test_fail_open_also_writes_audit_event(audit) -> None:  # type: ignore[no-untyped-def]
    """Even when fail_open allows the call, the audit row is still written."""
    inner_store = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=inner_store, batch_size=5, flush_interval_s=0.02
    )
    audit_mod = AuditModule(
        inner_store, secret=secrets.token_bytes(32), writer=writer
    )
    await audit_mod.start()
    try:
        cost = CostModule(
            audit_mod, store=_BrokenCostStore(), fail_open=True
        )
        cost.register(BudgetPolicy(agent_id="a", per_session_usd=1.0))
        await cost.check_or_raise("a", uuid4())
    finally:
        await audit_mod.close()
    events = [
        e
        for e in inner_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "budget.check_failed"
    ]
    assert len(events) == 1
    assert events[0].metadata["fail_mode"] == "open"
