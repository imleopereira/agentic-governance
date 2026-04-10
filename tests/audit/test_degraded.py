"""Tests for the graceful-degradation hotfix.

If the audit store is unreachable when a session begins, the audit module
must:
    1. NOT raise to the host application — log() must succeed
    2. Begin a new chain segment (prev_hash=None)
    3. Emit a ``chain.degraded_start`` marker event as the FIRST event in
       the segment, so auditors can find every chain discontinuity
    4. Allow subsequent events in the same session to link to the marker
       and continue the chain normally
"""
from __future__ import annotations

import secrets
from uuid import UUID

import pytest

from codeatelier_governance.audit import (
    AuditEvent,
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.audit.store import AuditStore


class _FailingPrimaryStore(InMemoryAuditStore):
    """In-memory store whose chain insert path always fails.

    Models the case where the primary Postgres is unreachable. The
    AuditModule should fall through to the BatchingWriter's in-memory
    fallback and emit a chain.degraded_start marker so auditors can find
    the discontinuity.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def insert_with_chain_lock(self, session_id: UUID, builder) -> None:  # type: ignore[override]
        self.calls += 1
        raise RuntimeError("simulated primary unreachable")


@pytest.mark.asyncio
async def test_log_succeeds_when_hydration_fails() -> None:
    """The host application call must NOT raise on degraded start."""
    primary: AuditStore = _FailingPrimaryStore()
    fallback = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=primary,
        fallback=fallback,
        batch_size=2,
        flush_interval_s=0.02,
    )
    audit = AuditModule(primary, secret=secrets.token_bytes(32), writer=writer)
    await audit.start()
    try:
        rec = await audit.log(AuditEvent(agent_id="a", kind="user.action"))
    finally:
        await audit.close()
    assert rec.kind == "user.action"


@pytest.mark.asyncio
async def test_chain_degraded_start_marker_is_emitted() -> None:
    """The first event in a degraded session is a chain.degraded_start marker."""
    primary = _FailingPrimaryStore()
    fallback = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=primary,
        fallback=fallback,
        batch_size=2,
        flush_interval_s=0.02,
    )
    audit = AuditModule(primary, secret=secrets.token_bytes(32), writer=writer)
    await audit.start()
    try:
        with audit.session():
            user_rec = await audit.log(AuditEvent(agent_id="a", kind="user.action"))
    finally:
        await audit.close()

    # Primary failed; events landed in fallback together with the marker.
    all_events = sorted(
        fallback._events.values(),  # type: ignore[attr-defined]
        key=lambda e: e.created_at,
    )
    kinds = [e.kind for e in all_events]
    assert "chain.degraded_start" in kinds
    marker = next(e for e in all_events if e.kind == "chain.degraded_start")
    assert marker.prev_hash is None
    assert marker.metadata["reason"] == "store unreachable at session start"
    # The user's event must link to the marker.
    assert user_rec.prev_hash == marker.hmac


@pytest.mark.asyncio
async def test_subsequent_events_link_to_marker_normally() -> None:
    """After degraded start, the chain continues normally for the same session."""
    primary: AuditStore = _FailingPrimaryStore()
    fallback = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=primary,
        fallback=fallback,
        batch_size=2,
        flush_interval_s=0.02,
    )
    audit = AuditModule(primary, secret=secrets.token_bytes(32), writer=writer)
    await audit.start()
    try:
        with audit.session():
            first = await audit.log(AuditEvent(agent_id="a", kind="one"))
            second = await audit.log(AuditEvent(agent_id="a", kind="two"))
            third = await audit.log(AuditEvent(agent_id="a", kind="three"))
    finally:
        await audit.close()
    assert second.prev_hash == first.hmac
    assert third.prev_hash == second.hmac


@pytest.mark.asyncio
async def test_marker_emitted_only_once_per_session() -> None:
    """The chain.degraded_start marker is emitted on the first event only."""
    primary = _FailingPrimaryStore()
    fallback = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=primary,
        fallback=fallback,
        batch_size=10,
        flush_interval_s=0.02,
    )
    audit = AuditModule(primary, secret=secrets.token_bytes(32), writer=writer)
    await audit.start()
    try:
        with audit.session():
            for _ in range(5):
                await audit.log(AuditEvent(agent_id="a", kind="event"))
    finally:
        await audit.close()
    markers = [
        e
        for e in fallback._events.values()  # type: ignore[attr-defined]
        if e.kind == "chain.degraded_start"
    ]
    assert len(markers) == 1


@pytest.mark.asyncio
async def test_normal_session_does_not_emit_marker() -> None:
    """Sanity check: clean store path does NOT emit a chain.degraded_start."""
    store = InMemoryAuditStore()
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02)
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    await audit.start()
    try:
        await audit.log(AuditEvent(agent_id="a", kind="event"))
    finally:
        await audit.close()
    markers = [
        e
        for e in store._events.values()  # type: ignore[attr-defined]
        if e.kind == "chain.degraded_start"
    ]
    assert markers == []
