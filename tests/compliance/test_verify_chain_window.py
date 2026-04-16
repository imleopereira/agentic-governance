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

    Logs 1500 events and confirms that the default chain-verification
    scopes to the most recent ``_CHAIN_VERIFY_WINDOW`` (1000) events by
    inspecting the arguments passed to ``verify_chain``.
    """
    await _log_n(audit, 1500)

    generator = ReportGenerator(
        audit_store=audit_store, audit_module=audit
    )
    seen_args: dict[str, int | None] = {}

    original = audit.verify_chain

    async def _spy(
        *,
        from_seq=None,
        to_seq=None,
        session_id=None,
    ):  # type: ignore[no-untyped-def]
        seen_args["from_seq"] = from_seq
        seen_args["to_seq"] = to_seq
        return await original(
            from_seq=from_seq, to_seq=to_seq, session_id=session_id
        )

    audit.verify_chain = _spy  # type: ignore[assignment,method-assign]
    try:
        status, fs, ts, _rot, _unres = (
            await generator.run_chain_verification_windowed()
        )
    finally:
        audit.verify_chain = original  # type: ignore[method-assign]

    assert status == "verified"
    assert seen_args["from_seq"] is not None
    assert seen_args["to_seq"] is not None
    # Window size must be exactly 1000 (inclusive bounds => 999 delta).
    assert seen_args["to_seq"] - seen_args["from_seq"] == 999  # type: ignore[operator]
    # And it must be anchored at the head of the chain.
    assert seen_args["to_seq"] == 1499
