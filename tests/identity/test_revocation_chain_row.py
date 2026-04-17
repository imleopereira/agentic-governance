"""v0.6.1 Track A finish: revocations emit chain rows.

When a key is revoked via ``RevocationStore.revoke_with_chain_event``,
the main audit chain gets an ``audit.agent_key_revocation`` event (with
its own HMAC + Ed25519 signature). This makes revocation tamper-evident
against the chain, not just the side table.
"""
from __future__ import annotations

import secrets

import pytest

from codeatelier_governance.audit.module import AuditModule
from codeatelier_governance.audit.store import InMemoryAuditStore
from codeatelier_governance.identity.keystore import EphemeralKeyStore
from codeatelier_governance.identity.revocation import RevocationStore
from codeatelier_governance.identity.signer import Ed25519Signer


@pytest.mark.asyncio
async def test_revocation_emits_chain_row() -> None:
    """A revocation call writes BOTH a chain event AND a mirror row."""
    store = InMemoryAuditStore()
    # Real Ed25519 key material via the ephemeral keystore (not a mock).
    keystore = EphemeralKeyStore(agent_id="governance.operator")
    signer = Ed25519Signer.from_private_key(keystore.load_private_key())
    audit = AuditModule(store, secret=secrets.token_bytes(32), signer=signer)
    await audit.start()
    try:
        revocations = RevocationStore()

        revoked_fp = "deadbeef" * 4
        record = await revocations.revoke_with_chain_event(
            audit_module=audit,
            key_fingerprint=revoked_fp,
            reason="suspected compromise on 2026-04-15",
            operator_id="security-on-call",
        )

        # Mirror row exists with chain_event_id populated.
        assert record.key_fingerprint == revoked_fp
        assert record.reason == "suspected compromise on 2026-04-15"
        assert record.operator_id == "security-on-call"
        assert record.chain_event_id is not None

        # A chain row with kind=audit.agent_key_revocation was written.
        # It is HMAC-signed (part of the chain) AND Ed25519-signed under
        # the currently-active key.
        events = list(store._events.values())
        assert len(events) == 1
        chain_event = events[0]
        assert chain_event.kind == "audit.agent_key_revocation"
        assert chain_event.metadata["revoked_key_fingerprint"] == revoked_fp
        assert chain_event.metadata["reason"] == (
            "suspected compromise on 2026-04-15"
        )
        assert chain_event.metadata["operator_id"] == "security-on-call"
        # Ed25519 signed by the active key.
        assert chain_event.signature is not None
        assert len(chain_event.signature) == 64
        assert chain_event.signature_status == "signed"
        assert chain_event.signing_key_fingerprint == signer.fingerprint

        # The mirror row's chain_event_id matches the emitted event.
        assert record.chain_event_id == chain_event.event_id

        # And the runtime revocation check honors the new row.
        assert revocations.is_revoked_at(revoked_fp, chain_seq=9999) is True
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_revocation_survives_chain_write_failure_in_lax_mode() -> None:
    """v0.6.2 Bug #9: strict_chain=False preserves the v0.6.1 degraded
    behavior. If the chain write fails, the revocation still lands in the
    mirror so runtime enforcement (``is_revoked_at``) keeps working. A
    distinct WARN is logged; no exception leaks into caller code.

    v0.6.2 flipped the default to strict_chain=True — tests must pass the
    opt-in explicitly.
    """
    class _BrokenAudit:
        """Audit surface whose .log() always raises."""

        _store = None  # no chain_seq lookup

        async def log(self, event):  # type: ignore[no-untyped-def]
            raise RuntimeError("audit substrate unreachable")

    revocations = RevocationStore(strict_chain=False)
    record = await revocations.revoke_with_chain_event(
        audit_module=_BrokenAudit(),
        key_fingerprint="a" * 64,
        reason="broken-path regression test",
        operator_id="on-call",
    )
    # No chain event id because the chain write failed — but the mirror
    # row still exists so the verify path rejects signed rows.
    assert record.chain_event_id is None
    assert revocations.is_revoked_at("a" * 64, chain_seq=0) is True
