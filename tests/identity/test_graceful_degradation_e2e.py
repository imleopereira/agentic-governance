"""CONSTRAINT #1 — end-to-end graceful degradation.

Wires an ``AuditModule`` to a signer whose ``sign()`` (and any proxied
keystore access) raises, then drives a full ``audit.log()`` call. Asserts:

  1. The call returns normally — no exception bubbles to the host.
  2. The row IS written to the underlying store.
  3. ``record.signature is None`` and
     ``record.signature_status == 'unsigned_local_failure'``.

This is THE load-bearing test for the v0.6 Cybersec override. Without this
guarantee, the F6 Track A work cannot ship: the whole point of the
sign-when-possible semantics is that agents behind unreadable key files
keep working.
"""
from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.audit.module import AuditModule
from codeatelier_governance.audit.store import InMemoryAuditStore
from codeatelier_governance.identity.keystore import KeyStoreError


class _ExplodingSigner:
    """Signer whose every ``sign()`` call raises ``KeyStoreError``.

    Emulates the scenario where a keystore backend loaded at SDK init time
    silently became unreadable later in the process life — the host call
    MUST continue working.
    """

    fingerprint = "deadbeefdeadbeefdeadbeefdeadbeef"

    def sign(self, canonical: bytes) -> bytes:
        raise KeyStoreError("key file unreadable at sign time (EACCES)")


@pytest.mark.asyncio
async def test_signer_failure_does_not_break_host_call() -> None:
    store = InMemoryAuditStore()
    audit = AuditModule(
        store,
        secret=secrets.token_bytes(32),
        signer=_ExplodingSigner(),
    )
    await audit.start()
    try:
        session_id = uuid4()
        # The critical assertion: this call MUST return normally.
        record = await audit.log(
            AuditEvent(
                agent_id="host-app-bot",
                kind="tool.call",
                session_id=session_id,
            )
        )
        # Row WAS written — audit is an observation surface, signing
        # failure never turns into an audit outage.
        assert record is not None
        assert not record.is_placeholder, (
            "degraded signing must NOT fall through to placeholder record; "
            "the host call's audit row is fully written, only the signature "
            "column degrades."
        )
        assert record.signature is None
        assert record.signing_key_fingerprint is None
        assert record.signature_status == "unsigned_local_failure"

        # And confirm the underlying store has it.
        fetched = await store.get_event(record.event_id)
        assert fetched is not None
        assert fetched.signature_status == "unsigned_local_failure"
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_chain_stays_intact_across_degraded_signing() -> None:
    """A degraded-signing row is still part of the HMAC chain.

    Constraint #1 + #7: ``unsigned_local_failure`` is NOT a chain break.
    Consecutive logs must still link via prev_hash.
    """
    store = InMemoryAuditStore()
    audit = AuditModule(
        store, secret=secrets.token_bytes(32), signer=_ExplodingSigner()
    )
    await audit.start()
    try:
        session_id = uuid4()
        first = await audit.log(
            AuditEvent(
                agent_id="a", kind="tool.call", session_id=session_id
            )
        )
        second = await audit.log(
            AuditEvent(
                agent_id="a", kind="tool.call", session_id=session_id
            )
        )
        assert second.prev_hash == first.hmac, "chain linkage unbroken"
        assert first.signature_status == "unsigned_local_failure"
        assert second.signature_status == "unsigned_local_failure"
    finally:
        await audit.close()
