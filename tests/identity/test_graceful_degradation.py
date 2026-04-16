"""CONSTRAINT #1 — graceful degradation.

If the signer raises for ANY reason (unreadable key file, missing env var,
corrupt PEM, broken keypair object), the chain write path MUST:

  1. Catch the exception.
  2. Write the row with ``signature_status='unsigned_local_failure'``.
  3. Return normally — no exception bubbles into host code.

We test this at the ``sign_audit_row`` helper level directly; that helper
is what the audit write path will call once wired in.  See the Track A
report for the wiring follow-up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from codeatelier_governance.audit.chain import sign_audit_row
from codeatelier_governance.identity.keystore import KeyStoreError


class _ExplodingSigner:
    """Signer stub whose ``sign`` always raises, emulating a failed keystore."""

    fingerprint = "deadbeef"

    def sign(self, canonical: bytes) -> bytes:
        raise KeyStoreError("key file unreadable: EACCES")


def _row() -> dict:
    return {
        "event_id": uuid4(),
        "session_id": uuid4(),
        "agent_id": "production-finance-bot",
        "parent_event_id": None,
        "kind": "tool.call",
        "model": None,
        "input_hash": None,
        "output_hash": None,
        "metadata": {},
        "prev_hash": None,
        "created_at": datetime.now(timezone.utc),
        "hmac": "0" * 64,
    }


def test_signer_failure_does_not_raise() -> None:
    sig, fp, status = sign_audit_row(_row(), _ExplodingSigner())
    assert sig is None
    assert fp is None
    assert status == "unsigned_local_failure"


def test_none_signer_returns_unsigned() -> None:
    sig, fp, status = sign_audit_row(_row(), None)
    assert sig is None
    assert fp is None
    assert status == "unsigned"


def test_arbitrary_exception_also_degrades() -> None:
    class Boom:
        fingerprint = "x"

        def sign(self, c: bytes) -> bytes:
            raise RuntimeError("something totally unexpected")

    sig, fp, status = sign_audit_row(_row(), Boom())
    assert status == "unsigned_local_failure"
    assert sig is None
