"""Tests for sdk.audit.verify_chain() and verify_chain_on_read option.

Item 5 requirements:
    1. verify_chain() on a clean chain → returns True
    2. verify_chain() on a chain with a tampered entry → raises ChainIntegrityError
       with the correct sequence number
    3. verify_chain_on_read=True: tamper one event, call get_events() → raises
       ChainIntegrityError
    4. verify_chain_on_read=False (default): same tampered event → events
       returned without error
    5. verify_chain(from_seq=N, to_seq=M) on a partial range → only verifies
       that window
"""
from __future__ import annotations

import secrets
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from codeatelier_governance.audit import AuditEvent, AuditModule
from codeatelier_governance.audit.errors import ChainIntegrityError
from codeatelier_governance.audit.models import AuditEventRecord
from codeatelier_governance.audit.store import BatchingWriter, InMemoryAuditStore


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _tamper(record: AuditEventRecord) -> AuditEventRecord:
    """Return a copy of *record* with a mutated ``kind`` field but the ORIGINAL
    hmac, simulating a database-level row edit that bypasses HMAC recomputation.
    """
    return AuditEventRecord(
        event_id=record.event_id,
        session_id=record.session_id,
        agent_id=record.agent_id,
        parent_event_id=record.parent_event_id,
        kind="tampered.kind",  # attacker rewrote this
        model=record.model,
        input_hash=record.input_hash,
        output_hash=record.output_hash,
        metadata=record.metadata,
        prev_hash=record.prev_hash,
        hmac=record.hmac,  # stale – no longer valid for the new kind
        created_at=record.created_at,
    )


@pytest_asyncio.fixture
async def secret_bytes() -> bytes:
    """32-byte HMAC secret for these tests."""
    return secrets.token_bytes(32)


@pytest_asyncio.fixture
async def store_with_events(
    secret_bytes: bytes,
) -> tuple[InMemoryAuditStore, UUID, AuditModule]:
    """Return a store pre-populated with 5 clean events, plus the AuditModule."""
    store = InMemoryAuditStore(max_events=10_000)
    writer = BatchingWriter(primary=store, batch_size=100, flush_interval_s=0.01)
    module = AuditModule(store, secret=secret_bytes, writer=writer)
    await module.start()

    session_id = uuid4()
    for i in range(5):
        await module.log(
            AuditEvent(
                session_id=session_id,
                agent_id="agent-x",
                kind=f"step.{i}",
            )
        )
    await module._writer.flush()
    return store, session_id, module


# ---------------------------------------------------------------------------
# Test 1: clean chain → True
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_chain_clean_returns_true(
    store_with_events: tuple[InMemoryAuditStore, UUID, AuditModule],
) -> None:
    """verify_chain() on an intact chain must return True."""
    store, session_id, module = store_with_events
    result = await module.verify_chain(session_id=session_id)
    assert result is True
    await module.close()


# ---------------------------------------------------------------------------
# Test 2: tampered entry → ChainIntegrityError with correct sequence number
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_chain_tampered_raises_with_seq(
    store_with_events: tuple[InMemoryAuditStore, UUID, AuditModule],
) -> None:
    """verify_chain() must raise ChainIntegrityError at the first bad link."""
    store, session_id, module = store_with_events

    # Tamper event at index 2 (0-based) directly in the store's _events dict.
    event_ids = store._by_session[session_id]
    target_id = event_ids[2]
    store._events[target_id] = _tamper(store._events[target_id])

    with pytest.raises(ChainIntegrityError) as exc_info:
        await module.verify_chain(session_id=session_id)

    # The error message must mention sequence 2.
    assert "2" in str(exc_info.value)
    await module.close()


# ---------------------------------------------------------------------------
# Test 3: verify_chain_on_read=True, tampered event → get_events() raises
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_events_raises_when_verify_on_read_and_tampered(
    secret_bytes: bytes,
) -> None:
    """get_events() with verify_chain_on_read=True must raise on tamper."""
    store = InMemoryAuditStore(max_events=10_000)
    writer = BatchingWriter(primary=store, batch_size=100, flush_interval_s=0.01)
    module = AuditModule(
        store,
        secret=secret_bytes,
        writer=writer,
        verify_chain_on_read=True,
    )
    await module.start()

    session_id = uuid4()
    for i in range(3):
        await module.log(
            AuditEvent(session_id=session_id, agent_id="agt", kind=f"ev.{i}")
        )
    await module._writer.flush()

    # Tamper the first stored event.
    first_id = store._by_session[session_id][0]
    store._events[first_id] = _tamper(store._events[first_id])

    with pytest.raises(ChainIntegrityError):
        await module.get_events(session_id)

    await module.close()


# ---------------------------------------------------------------------------
# Test 4: verify_chain_on_read=False (default) → events returned without error
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_events_no_error_when_verify_off_and_tampered(
    secret_bytes: bytes,
) -> None:
    """get_events() with verify_chain_on_read=False must not raise even on tamper."""
    store = InMemoryAuditStore(max_events=10_000)
    writer = BatchingWriter(primary=store, batch_size=100, flush_interval_s=0.01)
    module = AuditModule(
        store,
        secret=secret_bytes,
        writer=writer,
        verify_chain_on_read=False,  # explicit default
    )
    await module.start()

    session_id = uuid4()
    for i in range(3):
        await module.log(
            AuditEvent(session_id=session_id, agent_id="agt", kind=f"ev.{i}")
        )
    await module._writer.flush()

    # Tamper the first stored event.
    first_id = store._by_session[session_id][0]
    store._events[first_id] = _tamper(store._events[first_id])

    # Must NOT raise — tamper goes undetected when verify_chain_on_read is off.
    events = await module.get_events(session_id)
    assert len(events) == 3

    await module.close()


# ---------------------------------------------------------------------------
# Test 5: from_seq / to_seq partial range
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_chain_partial_range(
    store_with_events: tuple[InMemoryAuditStore, UUID, AuditModule],
) -> None:
    """verify_chain(from_seq=N, to_seq=M) only checks events [N..M]."""
    store, session_id, module = store_with_events

    # Tamper event at index 4 (last).
    event_ids = store._by_session[session_id]
    target_id = event_ids[4]
    store._events[target_id] = _tamper(store._events[target_id])

    # Verifying a window that does NOT include index 4 must pass.
    result = await module.verify_chain(
        session_id=session_id,
        from_seq=0,
        to_seq=3,
    )
    assert result is True

    # Verifying from index 4 must raise.
    with pytest.raises(ChainIntegrityError) as exc_info:
        await module.verify_chain(
            session_id=session_id,
            from_seq=4,
            to_seq=4,
        )
    assert "4" in str(exc_info.value)

    # Boundary: tamper at index 3 must be caught when to_seq=3 (the window
    # boundary includes the tampered event — not silently skipped).
    target_idx3 = event_ids[3]
    store._events[target_idx3] = _tamper(store._events[target_idx3])
    with pytest.raises(ChainIntegrityError):
        await module.verify_chain(
            session_id=session_id,
            from_seq=0,
            to_seq=3,
        )


# ---------------------------------------------------------------------------
# Test: head truncation (deleting the oldest events) is caught by genesis
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_chain_head_truncation_raises(
    store_with_events: tuple[InMemoryAuditStore, UUID, AuditModule],
) -> None:
    """Deleting the chain head (oldest event) must be caught by the genesis check.

    The surviving rows each still HMAC-verify and their mutual linkage is
    intact, so only the genesis check (the first row's prev_hash must be None)
    detects that the true first event was removed.
    """
    store, session_id, module = store_with_events

    event_ids = store._by_session[session_id]
    head_id = event_ids[0]
    del store._events[head_id]
    store._by_session[session_id] = event_ids[1:]

    with pytest.raises(ChainIntegrityError) as exc_info:
        await module.verify_chain(session_id=session_id)
    msg = str(exc_info.value).lower()
    assert "head truncated" in msg or "genesis" in msg
    await module.close()


# ---------------------------------------------------------------------------
# Test: verify_chain(session_id=None) must not return a vacuous True on a
# store that cannot enumerate all events (e.g. Postgres)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_chain_requires_session_id_for_non_inmemory_store(
    secret_bytes: bytes,
) -> None:
    """verify_chain() with no session_id must raise, not silently verify zero events."""

    class _NoIndexStore:
        # Deliberately lacks an ``_events`` attribute, like PostgresAuditStore.
        async def get_session_events(self, session_id: UUID) -> list[AuditEventRecord]:
            return []

        async def close(self) -> None:
            return None

    module = AuditModule(_NoIndexStore(), secret=secret_bytes)  # type: ignore[arg-type]
    try:
        with pytest.raises(ValueError, match="session_id is required"):
            await module.verify_chain()
    finally:
        await module.close()

    await module.close()
