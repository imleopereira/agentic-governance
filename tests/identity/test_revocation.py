"""CONSTRAINT #4 — revocation semantics.

A key revoked at chain_seq=100 must:
  * INVALIDATE rows signed by that key with chain_seq >= 100 (REVOKED_KEY).
  * LEAVE VALID rows signed with chain_seq < 100 (SIGNED).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ed25519

from codeatelier_governance.audit.chain import (
    sign_audit_row,
    verify_audit_row_signature,
)
from codeatelier_governance.identity.registry import AgentKeyRegistry
from codeatelier_governance.identity.revocation import RevocationStore
from codeatelier_governance.identity.signer import Ed25519Signer


def _row(chain_seq: int) -> dict:
    return {
        "event_id": uuid4(),
        "session_id": uuid4(),
        "agent_id": "prod-agent",
        "parent_event_id": None,
        "kind": "tool.call",
        "model": None,
        "input_hash": None,
        "output_hash": None,
        "metadata": {},
        "prev_hash": None,
        "created_at": datetime.now(timezone.utc),
        "hmac": f"{chain_seq:064x}",
        "chain_seq": chain_seq,
    }


def test_revocation_blocks_rows_at_or_after_revocation_seq() -> None:
    signer = Ed25519Signer.from_private_key(ed25519.Ed25519PrivateKey.generate())
    registry = AgentKeyRegistry()
    registry.register(
        key_fingerprint=signer.fingerprint,
        agent_id="prod-agent",
        public_key_pem=signer.public_key_pem,
        activated_at_chain_seq=0,
    )
    revocations = RevocationStore()

    earlier = _row(50)
    later = _row(200)
    sig_e, fp_e, _ = sign_audit_row(earlier, signer)
    sig_l, fp_l, _ = sign_audit_row(later, signer)

    # Before revocation: both rows verify.
    assert (
        verify_audit_row_signature(earlier, sig_e, fp_e, registry, revocations)
        == "signed"
    )
    assert (
        verify_audit_row_signature(later, sig_l, fp_l, registry, revocations)
        == "signed"
    )

    # Revoke at seq=100.
    revocations.revoke(
        key_fingerprint=signer.fingerprint,
        revoked_at_chain_seq=100,
        reason="key suspected leaked",
        operator_id="hmac-op-01",
    )

    # After revocation: earlier still SIGNED, later is REVOKED_KEY.
    assert (
        verify_audit_row_signature(earlier, sig_e, fp_e, registry, revocations)
        == "signed"
    )
    assert (
        verify_audit_row_signature(later, sig_l, fp_l, registry, revocations)
        == "revoked_key"
    )


def test_unknown_key_status() -> None:
    signer = Ed25519Signer.from_private_key(ed25519.Ed25519PrivateKey.generate())
    registry = AgentKeyRegistry()  # EMPTY
    row = _row(1)
    sig, fp, _ = sign_audit_row(row, signer)
    status = verify_audit_row_signature(row, sig, fp, registry, RevocationStore())
    assert status == "unknown_key"
