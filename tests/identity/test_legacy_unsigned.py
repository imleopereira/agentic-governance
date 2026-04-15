"""CONSTRAINT #7 — legacy_unsigned is NOT a chain break.

Rows with ``signature IS NULL`` and ``signing_key_fingerprint IS NULL``
are pre-v0.6 rows.  The verifier must return ``LEGACY_UNSIGNED`` rather
than flag the chain broken.  The compliance report will surface this as
a separate "% cryptographically signed" bucket, not as an integrity
violation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from codeatelier_governance.audit.chain import verify_audit_row_signature
from codeatelier_governance.identity.registry import AgentKeyRegistry


def _legacy_row() -> dict:
    return {
        "event_id": uuid4(),
        "session_id": uuid4(),
        "agent_id": "legacy-agent",
        "parent_event_id": None,
        "kind": "llm.call",
        "model": "gpt-4o",
        "input_hash": None,
        "output_hash": None,
        "metadata": {},
        "prev_hash": None,
        "created_at": datetime.now(timezone.utc),
        "hmac": "a" * 64,
        "chain_seq": 42,
    }


def test_null_signature_returns_legacy_unsigned() -> None:
    row = _legacy_row()
    status = verify_audit_row_signature(
        row, signature=None, fingerprint=None, registry=AgentKeyRegistry()
    )
    assert status == "legacy_unsigned"


def test_row_with_unsigned_status_honored() -> None:
    row = _legacy_row()
    row["signature_status"] = "unsigned"
    status = verify_audit_row_signature(
        row, signature=None, fingerprint=None, registry=AgentKeyRegistry()
    )
    # Row carries its own status -> honored.
    assert status == "unsigned"


def test_partial_nulls_are_invalid() -> None:
    """A row with a signature but no fingerprint (or vice versa) is
    malformed — we surface INVALID_SIGNATURE, never silently treat it
    as legacy.
    """
    row = _legacy_row()
    status = verify_audit_row_signature(
        row, signature=b"\x00" * 64, fingerprint=None, registry=AgentKeyRegistry()
    )
    assert status == "invalid_signature"
