"""Tests for the durable JSONL fallback store."""
from __future__ import annotations

import secrets
from pathlib import Path
from uuid import uuid4

import pytest

from codeatelier_governance.audit import (
    AuditEvent,
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.audit.jsonl_store import JsonlFallbackStore
from codeatelier_governance.audit.store import AuditStore


class _FailingPrimary(InMemoryAuditStore):
    """Primary that always fails the chain insert path."""

    async def insert_with_chain_lock(self, session_id, builder):  # type: ignore[no-untyped-def]
        raise RuntimeError("primary unreachable")


@pytest.mark.asyncio
async def test_jsonl_fallback_writes_to_disk_when_primary_down(
    tmp_path: Path,
) -> None:
    """When primary fails, audit events spill to JSONL — survive process exit."""
    fallback_path = tmp_path / "audit_fallback.jsonl"
    primary: AuditStore = _FailingPrimary()
    fallback = JsonlFallbackStore(fallback_path)
    writer = BatchingWriter(primary=primary, fallback=fallback)
    audit = AuditModule(
        primary, secret=secrets.token_bytes(32), writer=writer
    )
    await audit.start()
    try:
        with audit.session():
            for i in range(3):
                await audit.log(AuditEvent(agent_id="a", kind=f"event.{i}"))
    finally:
        await audit.close()

    assert fallback_path.exists()
    lines = [
        line
        for line in fallback_path.read_text().splitlines()
        if line.strip()
    ]
    # 3 user events + 1 chain.degraded_start marker (one shared session)
    assert len(lines) == 4
    assert any("chain.degraded_start" in line for line in lines)


@pytest.mark.asyncio
async def test_jsonl_fallback_chain_is_monotonic_within_session(
    tmp_path: Path,
) -> None:
    """Within a single session, the JSONL fallback maintains a valid chain."""
    fallback_path = tmp_path / "audit_fallback.jsonl"
    fallback = JsonlFallbackStore(fallback_path)

    sid = uuid4()
    secret_bytes = secrets.token_bytes(32)

    audit = AuditModule(
        _FailingPrimary(),
        secret=secret_bytes,
        writer=BatchingWriter(primary=_FailingPrimary(), fallback=fallback),
    )
    await audit.start()
    try:
        with audit.session(sid):
            r1 = await audit.log(AuditEvent(agent_id="a", kind="one"))
            r2 = await audit.log(AuditEvent(agent_id="a", kind="two"))
            r3 = await audit.log(AuditEvent(agent_id="a", kind="three"))
    finally:
        await audit.close()
    # The first user event links to the chain.degraded_start marker;
    # subsequent events link to each other.
    assert r2.prev_hash == r1.hmac
    assert r3.prev_hash == r2.hmac


@pytest.mark.asyncio
async def test_jsonl_fallback_drain_to_primary(tmp_path: Path) -> None:
    """drain_to() moves events from JSONL into the target and truncates."""
    fallback_path = tmp_path / "audit_fallback.jsonl"
    fallback = JsonlFallbackStore(fallback_path)

    # Write some events to the fallback
    audit = AuditModule(
        _FailingPrimary(),
        secret=secrets.token_bytes(32),
        writer=BatchingWriter(primary=_FailingPrimary(), fallback=fallback),
    )
    await audit.start()
    try:
        for i in range(5):
            await audit.log(AuditEvent(agent_id="a", kind=f"event.{i}"))
    finally:
        await audit.close()

    assert fallback_path.exists()
    # Drain into a fresh InMemoryAuditStore
    target = InMemoryAuditStore()
    drained = await fallback.drain_to(target)
    assert drained > 0
    # File should be gone
    assert not fallback_path.exists()
    # Target should have all the events
    assert await target.count() == drained


@pytest.mark.asyncio
async def test_jsonl_fallback_count_when_empty(tmp_path: Path) -> None:
    fallback = JsonlFallbackStore(tmp_path / "missing.jsonl")
    assert await fallback.count() == 0


# -- Gap #2: Read-only filesystem fallback to in-memory buffer -----------------


@pytest.mark.asyncio
async def test_jsonl_readonly_fs_falls_back_to_memory(tmp_path: Path) -> None:
    """When mkdir raises OSError, the store falls back to in-memory buffer.

    Events should still be written and drainable, they just won't survive
    a process restart.
    """
    # Use a path inside a non-existent read-only parent
    impossible_path = tmp_path / "readonly" / "nested" / "audit.jsonl"
    # Make the parent read-only so mkdir fails
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o444)

    try:
        store = JsonlFallbackStore(impossible_path)
        # The store should have detected the OS error and set _fs_available=False
        assert store._fs_available is False

        # Events should go to the in-memory buffer
        sid = uuid4()
        from codeatelier_governance.audit.models import AuditEventRecord
        from datetime import datetime

        record = AuditEventRecord(
            event_id=uuid4(),
            session_id=sid,
            agent_id="test",
            parent_event_id=None,
            kind="test.event",
            input_hash=None,
            output_hash=None,
            metadata={},
            prev_hash=None,
            hmac="a" * 64,
            created_at=datetime.now(),
        )
        await store.write_batch([record])
        assert len(store._memory_buffer) == 1

        # Drain to a target should move the memory events
        target = InMemoryAuditStore()
        drained = await store.drain_to(target)
        assert drained == 1
        assert len(store._memory_buffer) == 0
    finally:
        # Restore permissions for cleanup
        readonly_dir.chmod(0o755)
