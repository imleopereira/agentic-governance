"""v0.6.1 Track A finish: AuditModule.log signing happy path + degradation.

These tests assert the contract of ``AuditModule.log`` with a live Ed25519
signer (via ``EphemeralKeyStore`` — real key material, not a mock) and
with a signer whose ``sign`` method raises.

Coverage is intentionally narrow:
  (c) happy path — signer returns a 64-byte sig, status=signed, fp matches.
  (d) graceful degradation — signer raises → status=unsigned_local_failure.
"""
from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.audit.module import AuditModule
from codeatelier_governance.audit.store import InMemoryAuditStore
from codeatelier_governance.identity.keystore import (
    EphemeralKeyStore,
    KeyStoreError,
)
from codeatelier_governance.identity.signer import Ed25519Signer


@pytest.mark.asyncio
async def test_module_log_signs_with_ephemeral_keystore() -> None:
    """Constraint: real key material from EphemeralKeyStore produces a valid
    Ed25519 signature on every logged row."""
    keystore = EphemeralKeyStore(agent_id="billing-agent")
    signer = Ed25519Signer.from_private_key(keystore.load_private_key())

    store = InMemoryAuditStore()
    audit = AuditModule(store, secret=secrets.token_bytes(32), signer=signer)
    await audit.start()
    try:
        record = await audit.log(
            AuditEvent(
                agent_id="billing-agent",
                kind="tool.call",
                session_id=uuid4(),
            )
        )
        assert record.signature_status == "signed"
        assert record.signature is not None
        assert len(record.signature) == 64
        assert record.signing_key_fingerprint == signer.fingerprint

        # Round-trip verification: the stored signature verifies under the
        # keystore's public key. This proves the canonical-bytes contract
        # matches between sign and verify.
        from codeatelier_governance.audit.chain import verify_audit_row_signature
        from codeatelier_governance.identity.registry import AgentKeyRegistry

        registry = AgentKeyRegistry()
        registry.register(
            key_fingerprint=signer.fingerprint,
            agent_id="billing-agent",
            public_key_pem=signer.public_key_pem,
            activated_at_chain_seq=0,
        )
        row_dict = {
            "event_id": record.event_id,
            "session_id": record.session_id,
            "agent_id": record.agent_id,
            "parent_event_id": record.parent_event_id,
            "kind": record.kind,
            "model": record.model,
            "input_hash": record.input_hash,
            "output_hash": record.output_hash,
            "metadata": record.metadata,
            "prev_hash": record.prev_hash,
            "created_at": record.created_at,
            "hmac": record.hmac,
            "chain_seq": 0,
        }
        status = verify_audit_row_signature(
            row_dict,
            record.signature,
            record.signing_key_fingerprint,
            registry,
            None,
        )
        assert status == "signed"
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_module_log_degrades_on_keystore_failure() -> None:
    """Constraint #1: a signer whose ``sign`` raises produces
    ``signature_status='unsigned_local_failure'`` — never an exception."""

    class _BrokenSigner:
        """Mirrors a keystore whose hardware-backed key is unreachable."""

        fingerprint = "c" * 32
        public_key_pem = "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"

        def sign(self, canonical: bytes) -> bytes:
            raise KeyStoreError("HSM slot unreachable")

    store = InMemoryAuditStore()
    audit = AuditModule(
        store, secret=secrets.token_bytes(32), signer=_BrokenSigner()
    )
    await audit.start()
    try:
        # No exception must propagate into the caller's code path.
        record = await audit.log(
            AuditEvent(
                agent_id="billing-agent",
                kind="tool.call",
                session_id=uuid4(),
            )
        )
        assert record.signature is None
        assert record.signing_key_fingerprint is None
        assert record.signature_status == "unsigned_local_failure"
        # The HMAC chain is still intact — signing failure MUST NOT break
        # chain construction.
        assert record.hmac != "0" * 64
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_module_log_with_none_signer_returns_unsigned() -> None:
    """When no signer is configured at AuditModule construction time, rows
    carry ``signature_status='unsigned'`` (not 'legacy_unsigned' — that is
    reserved for pre-v0.6 rows read back from storage without signatures).
    """
    store = InMemoryAuditStore()
    audit = AuditModule(store, secret=secrets.token_bytes(32), signer=None)
    await audit.start()
    try:
        record = await audit.log(
            AuditEvent(
                agent_id="billing-agent",
                kind="tool.call",
                session_id=uuid4(),
            )
        )
        assert record.signature is None
        assert record.signing_key_fingerprint is None
        assert record.signature_status == "unsigned"
    finally:
        await audit.close()
