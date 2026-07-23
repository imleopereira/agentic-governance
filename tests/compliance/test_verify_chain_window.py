"""F4: window-limited chain verification.

DA blocker fix: ``verify_chain`` is O(n). For Article 12 retention
histories (6 months) running it over the full chain times out and is
a DoS vector. The ``from_seq``/``to_seq`` parameters let the compliance
report scope the verify to the most recent 1000 events by default.

These tests pin:
  * the window-limited call processes only the requested slice;
  * the off-by-one behavior of ``from_seq == to_seq`` (verifies exactly
    one event);
  * the default window in ``ReportGenerator._run_chain_verification``
    caps the scope to the last ``_CHAIN_VERIFY_WINDOW`` events.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from uuid import uuid4

from codeatelier_governance.audit import AuditEvent, AuditModule, InMemoryAuditStore
from codeatelier_governance.compliance.report import ReportGenerator


async def _log_n(audit: AuditModule, n: int) -> None:
    """Log ``n`` events into a single session so the linkage check passes."""
    sid = uuid4()
    for i in range(n):
        await audit.log(
            AuditEvent(
                agent_id="agent-a", kind="tool.call", session_id=sid
            )
        )


@pytest.mark.asyncio
async def test_verify_chain_full_vs_window(audit: AuditModule) -> None:
    """Full verify and windowed verify both pass on a healthy chain."""
    await _log_n(audit, 50)
    assert await audit.verify_chain() is True
    assert await audit.verify_chain(from_seq=0, to_seq=49) is True
    assert await audit.verify_chain(from_seq=0, to_seq=9) is True


@pytest.mark.asyncio
async def test_verify_chain_window_off_by_one(audit: AuditModule) -> None:
    """``from_seq == to_seq`` verifies exactly one event."""
    await _log_n(audit, 5)
    # Single-event window at each index should succeed.
    for i in range(5):
        assert await audit.verify_chain(from_seq=i, to_seq=i) is True


@pytest.mark.asyncio
async def test_verify_chain_window_rejects_inverted_range(
    audit: AuditModule,
) -> None:
    """from_seq > to_seq is a usage error, not silent success."""
    await _log_n(audit, 5)
    with pytest.raises(ValueError):
        await audit.verify_chain(from_seq=3, to_seq=1)


@pytest.mark.asyncio
async def test_verify_chain_window_processes_only_requested_slice(
    audit: AuditModule,
) -> None:
    """The windowed call must NOT iterate events outside the slice.

    We assert this by patching ``verify_event`` with a counting proxy
    and checking the call count against the window size.
    """
    await _log_n(audit, 500)

    calls: list[int] = [0]
    from codeatelier_governance.audit import chain as _chain

    real_verify_event = _chain.verify_event

    def _counting(record, secret):  # type: ignore[no-untyped-def]
        calls[0] += 1
        return real_verify_event(record, secret)

    with patch(
        "codeatelier_governance.audit.module.verify_event",
        side_effect=_counting,
    ):
        calls[0] = 0
        await audit.verify_chain(from_seq=0, to_seq=499)
        full = calls[0]

        calls[0] = 0
        await audit.verify_chain(from_seq=0, to_seq=99)
        windowed = calls[0]

    assert full == 500
    assert windowed == 100
    assert windowed < full


@pytest.mark.asyncio
async def test_report_generator_defaults_to_last_1000(
    audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    """ReportGenerator._run_chain_verification windows by default.

    Logs 1500 events and confirms the default chain-verification scopes to the
    most recent ``_CHAIN_VERIFY_WINDOW`` (1000) events, by inspecting the
    window bounds the verifier returns.
    """
    await _log_n(audit, 1500)

    generator = ReportGenerator(
        audit_store=audit_store, audit_module=audit
    )
    status, fs, ts, _rot, _unres = (
        await generator.run_chain_verification_windowed()
    )

    assert status == "verified"
    assert fs is not None and ts is not None
    # Window size must be exactly 1000 (inclusive bounds => 999 delta).
    assert ts - fs == 999
    # And it must be anchored at the head of the chain (0-based index 1499).
    assert ts == 1499


async def _log_session(audit: AuditModule, n: int, agent: str = "agent-a"):  # type: ignore[no-untyped-def]
    """Log ``n`` events into one fresh session; return the session_id."""
    sid = uuid4()
    for i in range(n):
        await audit.log(AuditEvent(agent_id=agent, kind=f"ev.{i}", session_id=sid))
    return sid


@pytest.mark.asyncio
async def test_compliance_detects_interior_deletion(
    audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    """The Article 12 verify must FAIL a chain with a deleted interior event.

    This is the round-3 blocker: per-row HMAC alone leaves survivors valid, so
    the per-session prev_hash->hmac linkage is what catches the deletion.
    """
    sid = await _log_session(audit, 5)
    generator = ReportGenerator(audit_store=audit_store, audit_module=audit)

    status, *_ = await generator.run_chain_verification_windowed()
    assert status == "verified"  # intact chain verifies

    # Delete an interior event (index 2) directly from the in-memory store.
    ids = list(audit_store._by_session[sid])
    del audit_store._events[ids[2]]
    audit_store._by_session[sid] = ids[:2] + ids[3:]

    status, *_ = await generator.run_chain_verification_windowed()
    assert status == "failed"  # linkage catches the gap left by the deletion


@pytest.mark.asyncio
async def test_compliance_no_false_alarm_on_intact_multi_session(
    audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    """Two intact sessions must verify (no cross-session-boundary false alarm)."""
    await _log_session(audit, 4)
    await _log_session(audit, 4)
    generator = ReportGenerator(audit_store=audit_store, audit_module=audit)

    status, *_ = await generator.run_chain_verification_windowed()
    assert status == "verified"


@pytest.mark.asyncio
async def test_compliance_detects_head_of_session_deletion(
    audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    """Deleting a session's FIRST (genesis) event must FAIL via the genesis check.

    from_seq defaults to 0 for a short chain, so genesis assertion is on; the
    surviving events still link cleanly, so only the genesis check catches it.
    """
    sid = await _log_session(audit, 5)
    generator = ReportGenerator(audit_store=audit_store, audit_module=audit)
    status, *_ = await generator.run_chain_verification_windowed()
    assert status == "verified"

    ids = list(audit_store._by_session[sid])
    del audit_store._events[ids[0]]  # delete the genesis event
    audit_store._by_session[sid] = ids[1:]

    status, *_ = await generator.run_chain_verification_windowed()
    assert status == "failed"


@pytest.mark.asyncio
async def test_compliance_empty_window_is_unverified_not_verified(
    audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    """A non-overlapping / empty verify window must be 'unverified', not vacuous 'verified'."""
    await _log_session(audit, 3)
    generator = ReportGenerator(audit_store=audit_store, audit_module=audit)

    status, *_ = await generator.run_chain_verification_windowed(
        from_seq=100, to_seq=200
    )
    assert status == "unverified"
