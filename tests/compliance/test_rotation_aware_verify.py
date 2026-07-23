"""BLOCKER C1: rotation-aware compliance chain verification.

Pins that ``ReportGenerator._run_chain_verification``:

  * Uses the legacy single-key verifier when the chain contains NO
    rotation marker rows. ``rotation_aware`` is ``False``,
    ``unresolved_fingerprints`` is ``[]``.
  * Uses ``verify_chain_with_rotation`` on a POSTGRES store when at least one
    ``audit.chain_key_rotation`` row is present (``rotation_aware`` True). An
    in-memory store verifies its own records regardless of markers
    (``rotation_aware`` False), so it can never be forced into a zero-row
    rotated verify.
  * Returns ``unverified`` (NOT ``verified`` / ``failed``) when the
    rotation-aware verifier reports unresolved key fingerprints, and
    surfaces those fingerprints in the result.
  * Returns ``verified`` when every key resolved and every row passed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from codeatelier_governance.audit import AuditEvent, AuditModule, InMemoryAuditStore
from codeatelier_governance.audit.chain import (
    KEY_ROTATION_KIND,
    ChainVerifyResult,
    ChainVerifyRow,
)
from codeatelier_governance.compliance.report import ReportGenerator


async def _real_rows(audit: AuditModule) -> list[ChainVerifyRow]:
    """Wrap the real logged in-memory records as ChainVerifyRows (valid linkage)."""
    records = await audit.list_all_records()
    return [
        ChainVerifyRow(chain_seq=i, record=r, hmac_next=None)
        for i, r in enumerate(records)
    ]


def _as_postgres_rotation(gen: ReportGenerator, rows: list[ChainVerifyRow]) -> None:
    """Drive a generator through the Postgres rotation branch with REAL rows.

    The rotation-aware path is Postgres-only (an in-memory store now verifies
    its own records and never routes here). We set a fake database_url so the
    in-memory hoist is skipped and mock the DB loaders to return the given
    non-empty rows, so verify_chain_with_rotation is fed real rows, not the
    secretly-empty list the old in-memory tests masked the branch with.
    """
    gen._database_url = "postgresql://fake/db"
    gen._has_rotation_markers = AsyncMock(return_value=True)  # type: ignore[method-assign]
    gen._load_key_versions = AsyncMock(return_value=[])  # type: ignore[method-assign]
    gen._build_uri_map = AsyncMock(return_value={})  # type: ignore[method-assign]
    gen._load_chain_rows = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    gen._current_chain_head = AsyncMock(  # type: ignore[method-assign]
        return_value=(len(rows) - 1 if rows else None)
    )


@pytest.mark.asyncio
async def test_single_key_chain_reports_rotation_aware_false(
    audit: AuditModule, audit_store: InMemoryAuditStore,
) -> None:
    sid = uuid4()
    for _ in range(5):
        await audit.log(AuditEvent(agent_id="a", kind="tool.call", session_id=sid))
    gen = ReportGenerator(audit_store=audit_store, audit_module=audit)
    report = await gen.generate_article12(verify_chain=True)
    assert report.rotation_aware is False
    assert report.unresolved_fingerprints == []
    assert report.chain_integrity_status == "verified"


@pytest.mark.asyncio
async def test_rotated_chain_uses_rotation_aware_path(
    audit: AuditModule, audit_store: InMemoryAuditStore,
) -> None:
    """A Postgres store with a rotation marker feeds the rotation-aware verifier
    the REAL (non-empty) loaded rows, then per-session linkage confirms it."""
    sid = uuid4()
    for _ in range(4):
        await audit.log(AuditEvent(agent_id="a", kind="tool.call", session_id=sid))
    rows = await _real_rows(audit)

    gen = ReportGenerator(audit_store=audit_store, audit_module=audit)
    _as_postgres_rotation(gen, rows)

    seen: dict[str, object] = {"rows": None}

    def fake_verify(rows_arg, key_versions, uri_map):  # noqa: ANN001
        seen["rows"] = rows_arg
        return ChainVerifyResult(
            verified=len(rows_arg), failed=0, unverified=0,
            status="ok", unresolved_fingerprints=[],
        )

    with patch(
        "codeatelier_governance.compliance.report.verify_chain_with_rotation",
        side_effect=fake_verify,
    ):
        status, _fs, _ts, rot, _unres = await gen.run_chain_verification_windowed()

    # De-mask: the verifier was fed a NON-EMPTY row set (the old in-memory test
    # secretly passed []).
    assert seen["rows"] is not None and len(seen["rows"]) == len(rows) > 0  # type: ignore[arg-type]
    assert rot is True
    assert status == "verified"


@pytest.mark.asyncio
async def test_rotated_chain_with_missing_old_key_returns_unverified(
    audit: AuditModule, audit_store: InMemoryAuditStore,
) -> None:
    """CRITICAL: missing key material MUST return unverified, NEVER verified."""
    sid = uuid4()
    for _ in range(3):
        await audit.log(AuditEvent(agent_id="a", kind="tool.call", session_id=sid))
    rows = await _real_rows(audit)

    gen = ReportGenerator(audit_store=audit_store, audit_module=audit)
    _as_postgres_rotation(gen, rows)

    def fake_verify(rows_arg, key_versions, uri_map):  # noqa: ANN001
        return ChainVerifyResult(
            verified=0, failed=0, unverified=1,
            status="unverified",
            unresolved_fingerprints=["fp-old-deadbeef"],
        )

    with patch(
        "codeatelier_governance.compliance.report.verify_chain_with_rotation",
        side_effect=fake_verify,
    ):
        status, _fs, _ts, rot, unres = await gen.run_chain_verification_windowed()

    assert rot is True
    assert status == "unverified"
    assert status != "verified"
    assert "fp-old-deadbeef" in unres


@pytest.mark.asyncio
async def test_rotated_chain_with_all_keys_verifies_correctly(
    audit: AuditModule, audit_store: InMemoryAuditStore,
) -> None:
    sid = uuid4()
    for _ in range(4):
        await audit.log(AuditEvent(agent_id="a", kind="tool.call", session_id=sid))
    rows = await _real_rows(audit)

    gen = ReportGenerator(audit_store=audit_store, audit_module=audit)
    _as_postgres_rotation(gen, rows)

    def fake_verify(rows_arg, key_versions, uri_map):  # noqa: ANN001
        return ChainVerifyResult(
            verified=len(rows_arg), failed=0, unverified=0,
            status="ok", unresolved_fingerprints=[],
        )

    with patch(
        "codeatelier_governance.compliance.report.verify_chain_with_rotation",
        side_effect=fake_verify,
    ):
        status, _fs, _ts, rot, unres = await gen.run_chain_verification_windowed()

    assert rot is True
    assert status == "verified"
    assert unres == []


@pytest.mark.asyncio
async def test_inmemory_rotation_marker_still_detects_deletion(
    audit: AuditModule, audit_store: InMemoryAuditStore,
) -> None:
    """Exploit closed: an IN-MEMORY store with a rotation marker must NOT
    vacuously verify. Logging a kind='audit.chain_key_rotation' event used to
    route the in-memory store into the Postgres rotated branch, which loaded
    zero rows and returned 'verified'. In-memory now verifies its own records
    (per-session linkage), so a deletion is caught."""
    sid = uuid4()
    await audit.log(AuditEvent(
        agent_id="a", kind=KEY_ROTATION_KIND, session_id=sid,
        metadata={"outgoing_fingerprint": "fp-old", "incoming_fingerprint": "fp-new"},
    ))
    for _ in range(4):
        await audit.log(AuditEvent(agent_id="a", kind="tool.call", session_id=sid))

    gen = ReportGenerator(audit_store=audit_store, audit_module=audit)

    status, _fs, _ts, rot, _unres = await gen.run_chain_verification_windowed()
    assert status == "verified"   # intact
    assert rot is False           # in-memory does NOT use the rotation-aware path

    # Delete an interior event; the report must now FAIL, not vacuously verify.
    ids = list(audit_store._by_session[sid])
    del audit_store._events[ids[2]]
    audit_store._by_session[sid] = ids[:2] + ids[3:]

    status, *_ = await gen.run_chain_verification_windowed()
    assert status == "failed"


@pytest.mark.asyncio
async def test_rotated_branch_detects_interior_deletion(
    audit: AuditModule, audit_store: InMemoryAuditStore,
) -> None:
    """The Postgres rotation branch FAILS on an interior-deleted row set.

    Drives the real rotated path (verify_chain_with_rotation mocked "ok") over
    rows with a middle row removed, so per-session linkage must catch the gap.
    """
    sid = uuid4()
    for _ in range(5):
        await audit.log(AuditEvent(agent_id="a", kind="tool.call", session_id=sid))
    rows = await _real_rows(audit)
    gapped = rows[:2] + rows[3:]  # drop the middle row -> broken linkage

    gen = ReportGenerator(audit_store=audit_store, audit_module=audit)
    _as_postgres_rotation(gen, gapped)

    def fake_verify(rows_arg, key_versions, uri_map):  # noqa: ANN001
        return ChainVerifyResult(
            verified=len(rows_arg), failed=0, unverified=0,
            status="ok", unresolved_fingerprints=[],
        )

    with patch(
        "codeatelier_governance.compliance.report.verify_chain_with_rotation",
        side_effect=fake_verify,
    ):
        status, _fs, _ts, rot, _unres = await gen.run_chain_verification_windowed()

    assert rot is True
    assert status == "failed"
