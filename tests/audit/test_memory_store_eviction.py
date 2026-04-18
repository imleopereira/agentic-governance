"""v0.6.2 Bug #11 — InMemoryAuditStore must not silently evict.

Before v0.6.2, the in-memory store popped the oldest event on overflow
at ``max_events=100_000`` with no log, no counter, no marker. Tests
running against this store gave false-green on any code path that would
break in Postgres (which enforces append-only via triggers). A session
with > ``max_events`` events had its HMAC chain silently truncated.

v0.6.2 flips the default to ``on_full="raise"`` — writes past the cap
raise ``StoreUnavailableError`` so the bug surfaces immediately. Ring-
buffer semantics remain available via an explicit
``on_full="evict"`` opt-in (used by the ``BatchingWriter`` degraded-
mode fallback and the ``enable_audit=False`` SDK path), and when that
mode is active every eviction:

  * emits a ``audit.memory_store_evicted`` structlog WARN with the
    evicted event id and running eviction count, and
  * increments the counter returned by ``InMemoryAuditStore.stats()``.

This test file exercises both modes end-to-end with REAL audit events
flowing through the chain-write path (``insert_with_chain_lock``), not
just direct store writes — that's the path where false-greens used to
hide.
"""
from __future__ import annotations

import secrets
from uuid import uuid4

import pytest
import structlog

from codeatelier_governance.audit import AuditEvent, AuditModule
from codeatelier_governance.audit.errors import (
    ChainIntegrityError,
    StoreUnavailableError,
)
from codeatelier_governance.audit.store import InMemoryAuditStore


@pytest.mark.asyncio
async def test_default_on_full_raises_on_overflow() -> None:
    """Default on_full='raise' refuses to silently truncate the chain.

    Hits the store directly via ``insert_with_chain_lock`` so we
    observe the raise at the store boundary. (The ``AuditModule.log``
    path catches ``StoreUnavailableError`` and falls through to a
    fallback store by design — architectural invariant #1: the host
    app must keep working if the audit DB is unreachable. That
    fallback path is tested elsewhere; here we pin the store-level
    contract that v0.6.2 Bug #11 introduces.)
    """
    cap = 5
    store = InMemoryAuditStore(max_events=cap)  # default on_full='raise'
    secret = secrets.token_bytes(32)
    audit = AuditModule(store, secret=secret)
    await audit.start()
    try:
        session = uuid4()
        for i in range(cap):
            await audit.log(
                AuditEvent(
                    session_id=session,
                    agent_id="agent-overflow",
                    kind=f"step.{i}",
                )
            )
        # Go straight at the store — ``write_batch`` is the simplest
        # path that doesn't go through the AuditModule's degraded-mode
        # fallback. Any well-formed append past the cap raises.
        existing = next(iter(store._events.values()))
        overflow_event = type(existing)(
            event_id=uuid4(),
            session_id=session,
            agent_id="agent-overflow",
            parent_event_id=None,
            kind="step.overflow",
            model=None,
            input_hash=None,
            output_hash=None,
            metadata={},
            prev_hash=None,
            hmac="0" * 64,
            created_at=existing.created_at,
        )
        with pytest.raises(StoreUnavailableError):
            await store.write_batch([overflow_event])
        # The chain that landed is intact and verify_chain reports
        # clean — nothing was silently dropped.
        assert await audit.verify_chain(session_id=session) is True
        assert store.stats()["evicted_total"] == 0
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_evict_mode_warns_and_increments_counter() -> None:
    """Opt-in on_full='evict' emits a WARN and bumps stats().evicted_total."""
    cap = 5
    store = InMemoryAuditStore(max_events=cap, on_full="evict")
    audit = AuditModule(store, secret=secrets.token_bytes(32))
    await audit.start()
    try:
        session = uuid4()
        # Write cap + 1 real chain events. The (cap+1)-th triggers one
        # eviction of the oldest.
        with structlog.testing.capture_logs() as captured:
            for i in range(cap + 1):
                await audit.log(
                    AuditEvent(
                        session_id=session,
                        agent_id="agent-ring",
                        kind=f"step.{i}",
                    )
                )

        # Counter reflects exactly one eviction.
        stats = store.stats()
        assert stats["evicted_total"] == 1
        assert stats["max_events"] == cap
        assert stats["on_full"] == "evict"
        assert stats["size"] == cap

        # WARN was emitted exactly once with the expected payload shape.
        evictions = [
            entry
            for entry in captured
            if entry.get("event") == "audit.memory_store_evicted"
        ]
        assert len(evictions) == 1
        assert evictions[0].get("log_level") == "warning"
        assert evictions[0].get("evicted_count") == 1
        assert evictions[0].get("max_events") == cap
        # The oldest_event_id field is populated with a UUID string
        # (format is an implementation detail — we just check it's non-
        # empty and stringified).
        oldest = evictions[0].get("oldest_event_id")
        assert isinstance(oldest, str) and len(oldest) > 0
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_evict_mode_truncation_surfaces_explicitly_to_verifier() -> None:
    """After eviction, chain verification reports truncation explicitly.

    The existing ``AuditModule.verify_chain`` only walks consecutive
    events returned by ``get_session_events`` — it cannot "see" that
    earlier events were dropped by a ring buffer. v0.6.2 closes the gap
    by making truncation an explicit signal on the store itself:
    ``store.verify_not_truncated()`` raises ``ChainIntegrityError`` when
    any eviction has happened. A caller who wants a full proof calls
    BOTH methods.

    The key property this test asserts: after eviction, the chain
    verifier path does NOT pass as a fully-verified chain.
    """
    cap = 3
    store = InMemoryAuditStore(max_events=cap, on_full="evict")
    audit = AuditModule(store, secret=secrets.token_bytes(32))
    await audit.start()
    try:
        session = uuid4()
        # Overflow by 2 to force 2 evictions.
        for i in range(cap + 2):
            await audit.log(
                AuditEvent(
                    session_id=session,
                    agent_id="agent-trunc",
                    kind=f"step.{i}",
                )
            )
        assert store.stats()["evicted_total"] == 2

        # The store-level chain verifier hook refuses to pass the
        # truncated chain as whole.
        with pytest.raises(ChainIntegrityError) as exc_info:
            store.verify_not_truncated()
        # The message names the feature so an operator reading the
        # traceback knows what happened.
        assert "truncat" in str(exc_info.value).lower()
        # And the count of dropped events is surfaced.
        assert "2" in str(exc_info.value)
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_verify_not_truncated_passes_when_no_evictions() -> None:
    """Sanity: with no evictions, the hook returns True."""
    store = InMemoryAuditStore(max_events=1000, on_full="evict")
    assert store.stats()["evicted_total"] == 0
    assert store.verify_not_truncated() is True


@pytest.mark.asyncio
async def test_stats_shape_is_stable() -> None:
    """stats() exposes the minimum documented keys."""
    store = InMemoryAuditStore(max_events=10, on_full="raise")
    stats = store.stats()
    # Minimum required keys per the v0.6.2 spec.
    assert "evicted_total" in stats
    assert stats["evicted_total"] == 0
    # Helpful auxiliaries — useful for dashboards and test assertions.
    assert stats["max_events"] == 10
    assert stats["on_full"] == "raise"
    assert stats["size"] == 0
