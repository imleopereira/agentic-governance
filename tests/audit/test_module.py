"""Tests for AuditModule.log / trace + session context propagation."""
from __future__ import annotations

import asyncio

import pytest

from codeatelier_governance.audit import (
    AuditEvent,
    AuditModule,
    ChainIntegrityError,
    InMemoryAuditStore,
)


@pytest.mark.asyncio
async def test_log_returns_record_with_chain_fields(audit: AuditModule) -> None:
    record = await audit.log(AuditEvent(agent_id="a", kind="k"))
    assert record.event_id is not None
    assert record.session_id is not None
    assert record.hmac and len(record.hmac) == 64
    assert record.prev_hash is None  # first event in a fresh session


@pytest.mark.asyncio
async def test_chain_links_subsequent_events(audit: AuditModule) -> None:
    with audit.session() as sid:
        first = await audit.log(AuditEvent(agent_id="a", kind="one"))
        second = await audit.log(AuditEvent(agent_id="a", kind="two"))
        third = await audit.log(AuditEvent(agent_id="a", kind="three"))
    assert first.session_id == sid
    assert second.session_id == sid
    assert third.session_id == sid
    assert second.prev_hash == first.hmac
    assert third.prev_hash == second.hmac


@pytest.mark.asyncio
async def test_trace_returns_provenance_chain(audit: AuditModule) -> None:
    with audit.session():
        root = await audit.log(AuditEvent(agent_id="a", kind="root"))
        child = await audit.log(
            AuditEvent(agent_id="a", kind="child", parent_event_id=root.event_id)
        )
        grandchild = await audit.log(
            AuditEvent(agent_id="a", kind="gc", parent_event_id=child.event_id)
        )
    # Flush the batching writer so the store is populated
    await audit._writer.flush()
    chain = await audit.trace(grandchild.event_id)
    assert [c.kind for c in chain] == ["root", "child", "gc"]


@pytest.mark.asyncio
async def test_trace_detects_tampered_chain(audit: AuditModule) -> None:
    record = await audit.log(AuditEvent(agent_id="a", kind="k"))
    await audit._writer.flush()
    # Reach into the in-memory store and corrupt one row.
    store = audit._store  # type: ignore[attr-defined]
    assert isinstance(store, InMemoryAuditStore)
    stored = store._events[record.event_id]  # type: ignore[attr-defined]
    # Replace with a record that has the right id but wrong metadata.
    from codeatelier_governance.audit.models import AuditEventRecord

    tampered = AuditEventRecord(
        event_id=stored.event_id,
        session_id=stored.session_id,
        agent_id="evil",  # changed
        parent_event_id=stored.parent_event_id,
        kind=stored.kind,
        input_hash=stored.input_hash,
        output_hash=stored.output_hash,
        metadata=stored.metadata,
        prev_hash=stored.prev_hash,
        hmac=stored.hmac,
        created_at=stored.created_at,
    )
    store._events[record.event_id] = tampered  # type: ignore[attr-defined]
    with pytest.raises(ChainIntegrityError):
        await audit.trace(record.event_id)


@pytest.mark.asyncio
async def test_concurrent_logs_keep_chain_monotonic(audit: AuditModule) -> None:
    with audit.session():
        # Fire 50 concurrent logs in the same session.
        results = await asyncio.gather(
            *[audit.log(AuditEvent(agent_id="a", kind=f"k{i}")) for i in range(50)]
        )
    # Every record except the first must have a non-null prev_hash.
    nulls = [r for r in results if r.prev_hash is None]
    assert len(nulls) == 1, "exactly one event should have a null prev_hash"
    # Each prev_hash should match some other record's hmac in the set.
    hmacs = {r.hmac for r in results}
    for r in results:
        if r.prev_hash is not None:
            assert r.prev_hash in hmacs


@pytest.mark.asyncio
async def test_log_validates_session_context(audit: AuditModule) -> None:
    # Outside a session context, a fresh session_id is generated per log.
    a = await audit.log(AuditEvent(agent_id="a", kind="k"))
    b = await audit.log(AuditEvent(agent_id="a", kind="k"))
    assert a.session_id != b.session_id
