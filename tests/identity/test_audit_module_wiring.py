"""F6 Track A wiring: AuditModule actually calls the signer.

Constructs an AuditModule with an Ed25519 signer and asserts that a logged
row carries ``signature != None``, ``signature_status == 'signed'``, and a
fingerprint matching the signer's public key.
"""
from __future__ import annotations

import secrets
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.audit.module import AuditModule
from codeatelier_governance.audit.store import InMemoryAuditStore
from codeatelier_governance.identity.signer import Ed25519Signer


@pytest.mark.asyncio
async def test_audit_row_is_signed_when_signer_configured() -> None:
    store = InMemoryAuditStore()
    signer = Ed25519Signer.from_private_key(ed25519.Ed25519PrivateKey.generate())
    audit = AuditModule(store, secret=secrets.token_bytes(32), signer=signer)
    await audit.start()
    try:
        session_id = uuid4()
        record = await audit.log(
            AuditEvent(
                agent_id="wiring-test-agent",
                kind="tool.call",
                session_id=session_id,
            )
        )
        assert record.signature is not None, "signature must be populated"
        assert len(record.signature) == 64, "Ed25519 sig is 64 bytes"
        assert record.signature_status == "signed"
        assert record.signing_key_fingerprint == signer.fingerprint

        # And the in-store row carries the same values.
        fetched = await store.get_event(record.event_id)
        assert fetched is not None
        assert fetched.signature == record.signature
        assert fetched.signature_status == "signed"
        assert fetched.signing_key_fingerprint == signer.fingerprint
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_audit_row_unsigned_when_no_signer() -> None:
    """Back-compat: with signer=None every row is written unsigned."""
    store = InMemoryAuditStore()
    audit = AuditModule(store, secret=secrets.token_bytes(32))
    await audit.start()
    try:
        record = await audit.log(
            AuditEvent(agent_id="no-signer-agent", kind="tool.call")
        )
        assert record.signature is None
        assert record.signing_key_fingerprint is None
        assert record.signature_status == "unsigned"
    finally:
        await audit.close()
