"""BLOCKER C1: rotation-aware compliance chain verification.

Pins that ``ReportGenerator._run_chain_verification``:

  * Uses the legacy single-key verifier when the chain contains NO
    rotation marker rows. ``rotation_aware`` is ``False``,
    ``unresolved_fingerprints`` is ``[]``.
  * Uses ``verify_chain_with_rotation`` when at least one
    ``audit.chain_key_rotation`` row is present. ``rotation_aware`` is
    ``True``.
  * Returns ``unverified`` (NOT ``verified`` / ``failed``) when the
    rotation-aware verifier reports unresolved key fingerprints, and
    surfaces those fingerprints in the result.
  * Returns ``verified`` when every key resolved and every row passed.
"""
from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest

from codeatelier_governance.audit import AuditEvent, AuditModule, InMemoryAuditStore
from codeatelier_governance.audit.chain import (
    KEY_ROTATION_KIND,
    ChainVerifyResult,
)
from codeatelier_governance.compliance.report import ReportGenerator


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
    """Seed a rotation marker; assert the rotation-aware verifier is invoked."""
    sid = uuid4()
    # A real rotation marker row carries kind == KEY_ROTATION_KIND. The
    # in-memory chain check would normally reject an arbitrary kind, so
    # we mock the rotation-aware path while leaving the marker detection
    # path real.
    await audit.log(AuditEvent(
        agent_id="a", kind=KEY_ROTATION_KIND, session_id=sid,
        metadata={"outgoing_fingerprint": "fp-old", "incoming_fingerprint": "fp-new"},
    ))
    await audit.log(AuditEvent(agent_id="a", kind="tool.call", session_id=sid))

    gen = ReportGenerator(audit_store=audit_store, audit_module=audit)

    called = {"n": 0}

    def fake_verify(rows, key_versions, uri_map):  # noqa: ANN001
        called["n"] += 1
        return ChainVerifyResult(
            verified=len(rows), failed=0, unverified=0,
            status="ok", unresolved_fingerprints=[],
        )

    with patch(
        "codeatelier_governance.compliance.report.verify_chain_with_rotation",
        side_effect=fake_verify,
    ):
        report = await gen.generate_article12(verify_chain=True)

    assert called["n"] == 1, "rotation-aware verifier was not invoked"
    assert report.rotation_aware is True


@pytest.mark.asyncio
async def test_rotated_chain_with_missing_old_key_returns_unverified(
    audit: AuditModule, audit_store: InMemoryAuditStore,
) -> None:
    """CRITICAL: missing key material MUST return unverified, NEVER verified."""
    sid = uuid4()
    await audit.log(AuditEvent(
        agent_id="a", kind=KEY_ROTATION_KIND, session_id=sid,
        metadata={"outgoing_fingerprint": "fp-old", "incoming_fingerprint": "fp-new"},
    ))
    gen = ReportGenerator(audit_store=audit_store, audit_module=audit)

    def fake_verify(rows, key_versions, uri_map):  # noqa: ANN001
        return ChainVerifyResult(
            verified=0, failed=0, unverified=1,
            status="unverified",
            unresolved_fingerprints=["fp-old-deadbeef"],
        )

    with patch(
        "codeatelier_governance.compliance.report.verify_chain_with_rotation",
        side_effect=fake_verify,
    ):
        report = await gen.generate_article12(verify_chain=True)

    assert report.rotation_aware is True
    assert report.chain_integrity_status == "unverified"
    assert report.chain_integrity_status != "verified"
    assert "fp-old-deadbeef" in report.unresolved_fingerprints


@pytest.mark.asyncio
async def test_rotated_chain_with_all_keys_verifies_correctly(
    audit: AuditModule, audit_store: InMemoryAuditStore,
) -> None:
    sid = uuid4()
    await audit.log(AuditEvent(
        agent_id="a", kind=KEY_ROTATION_KIND, session_id=sid,
        metadata={"outgoing_fingerprint": "fp-old", "incoming_fingerprint": "fp-new"},
    ))
    await audit.log(AuditEvent(agent_id="a", kind="tool.call", session_id=sid))
    gen = ReportGenerator(audit_store=audit_store, audit_module=audit)

    def fake_verify(rows, key_versions, uri_map):  # noqa: ANN001
        return ChainVerifyResult(
            verified=len(rows), failed=0, unverified=0,
            status="ok", unresolved_fingerprints=[],
        )

    with patch(
        "codeatelier_governance.compliance.report.verify_chain_with_rotation",
        side_effect=fake_verify,
    ):
        report = await gen.generate_article12(verify_chain=True)

    assert report.rotation_aware is True
    assert report.chain_integrity_status == "verified"
    assert report.unresolved_fingerprints == []
