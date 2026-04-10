"""Tests for BatchingWriter — size flush, time flush, degraded mode."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from codeatelier_governance.audit import BatchingWriter, InMemoryAuditStore
from codeatelier_governance.audit.errors import StoreUnavailableError
from codeatelier_governance.audit.models import AuditEventRecord
from codeatelier_governance.audit.store import AuditStore


def _record() -> AuditEventRecord:
    return AuditEventRecord(
        event_id=uuid4(),
        session_id=uuid4(),
        agent_id="a",
        parent_event_id=None,
        kind="k",
        input_hash=None,
        output_hash=None,
        metadata={},
        prev_hash=None,
        hmac="a" * 64,
        created_at=datetime.now(timezone.utc),
    )


class FailingStore(AuditStore):
    """Always errors on write — simulates a downed primary."""

    def __init__(self) -> None:
        self.attempts = 0

    async def insert_with_chain_lock(self, session_id, builder):  # type: ignore[no-untyped-def]
        raise RuntimeError("primary unavailable")

    async def write_batch(self, events):  # type: ignore[no-untyped-def]
        self.attempts += 1
        raise RuntimeError("primary unavailable")

    async def get_event(self, event_id):  # type: ignore[no-untyped-def]
        return None

    async def get_chain(self, event_id):  # type: ignore[no-untyped-def]
        return []

    async def get_last_hmac(self, session_id):  # type: ignore[no-untyped-def]
        return None


@pytest.mark.asyncio
async def test_size_based_flush() -> None:
    primary = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=primary, batch_size=3, flush_interval_s=10.0
    )
    await writer.start()
    try:
        for _ in range(3):
            await writer.enqueue(_record())
        # Wait briefly for the flush to run
        for _ in range(20):
            if await primary.count() == 3:
                break
            await asyncio.sleep(0.02)
        assert await primary.count() == 3
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_time_based_flush() -> None:
    primary = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=primary, batch_size=1000, flush_interval_s=0.05
    )
    await writer.start()
    try:
        await writer.enqueue(_record())
        # Wait for the timer to elapse and flush
        for _ in range(20):
            if await primary.count() == 1:
                break
            await asyncio.sleep(0.02)
        assert await primary.count() == 1
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_degraded_mode_falls_back() -> None:
    primary = FailingStore()
    fallback = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=primary,
        fallback=fallback,
        batch_size=2,
        flush_interval_s=0.05,
    )
    await writer.start()
    try:
        await writer.enqueue(_record())
        await writer.enqueue(_record())
        for _ in range(30):
            if await fallback.count() == 2 and writer.degraded:
                break
            await asyncio.sleep(0.02)
        assert writer.degraded is True
        assert await fallback.count() == 2
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_buffer_overflow_spills_to_fallback_directly() -> None:
    primary = FailingStore()
    fallback = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=primary,
        fallback=fallback,
        batch_size=10_000,  # never trigger size flush
        flush_interval_s=10.0,  # never trigger time flush
        buffer_max=3,
    )
    await writer.start()
    try:
        # First 3 fit in buffer
        for _ in range(3):
            await writer.enqueue(_record())
        # The 4th has nowhere to go in buffer → spilled directly to fallback
        await writer.enqueue(_record())
        assert await fallback.count() >= 1
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_close_drains_buffer() -> None:
    primary = InMemoryAuditStore()
    writer = BatchingWriter(
        primary=primary, batch_size=10_000, flush_interval_s=10.0
    )
    await writer.start()
    await writer.enqueue(_record())
    await writer.enqueue(_record())
    await writer.close()
    assert await primary.count() == 2


@pytest.mark.asyncio
async def test_enqueue_after_close_raises() -> None:
    primary = InMemoryAuditStore()
    writer = BatchingWriter(primary=primary)
    await writer.start()
    await writer.close()
    with pytest.raises(StoreUnavailableError):
        await writer.enqueue(_record())
