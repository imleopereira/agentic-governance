"""v0.6.2 Bug #9 — revocation must not silently write an unlinked row.

Before v0.6.2, ``RevocationStore.revoke_with_chain_event`` swallowed any
failure from ``audit_module.log()``, logged WARN, and then appended the
revocation to the side table with ``chain_event_id=None``. A malicious
operator who could momentarily suppress audit writes (kill signal on the
audit worker, transient DB hiccup) could use this to land a revocation
that is NOT cross-bound to the chain — precisely the tamper surface
``revoke_with_chain_event`` was designed to close.

v0.6.2 flips the default: when ``strict_chain=True`` (the new default),
the chain-write failure RAISES and no revocation row is written. The
caller sees the degraded audit substrate instead of a silently unlinked
revocation.

Lax mode (``strict_chain=False``) preserves the old behavior for
disaster-recovery scenarios where the operator has explicitly accepted
the broken linkage; a DISTINCT structured-log event
(``identity.revocation_without_chain_event``) is emitted so SIEM rules
can alert on any revocation that lands without a chain linkage.

Sibling note (see .agents/cybersecurity.md v0.6.2): "admin-bypass paths
missing distinct audit markers" and "rotation-unaware verifiers" are
siblings — both are silent failures of the story the security feature
depends on not being silent.
"""
from __future__ import annotations

import pytest
import structlog

from codeatelier_governance.identity.revocation import RevocationStore


class _BrokenAudit:
    """Audit surface whose .log() always raises.

    Mimics a transient failure of the audit worker — the kind of
    short-lived outage that Bug #9 allowed an attacker to exploit as a
    "land a revocation without a chain event" window.
    """

    _store = None  # no chain_seq lookup

    async def log(self, event):  # type: ignore[no-untyped-def]
        raise RuntimeError("audit substrate unreachable")


@pytest.mark.asyncio
async def test_strict_chain_default_raises_on_chain_write_failure() -> None:
    """Default is strict — a failed chain write RAISES and writes nothing."""
    revocations = RevocationStore()  # default strict_chain=True
    broken_fp = "a" * 64

    with pytest.raises(RuntimeError, match="audit substrate unreachable"):
        await revocations.revoke_with_chain_event(
            audit_module=_BrokenAudit(),
            key_fingerprint=broken_fp,
            reason="chain-write-failure regression",
            operator_id="on-call",
        )

    # The revocation row was NOT written. A runtime verifier asking
    # "is this fingerprint revoked?" correctly answers False — the
    # alternative (row written with chain_event_id=None) would have
    # silently enforced a revocation with no tamper-evident linkage.
    assert revocations.is_revoked_at(broken_fp, chain_seq=9999) is False
    assert revocations.all() == []


@pytest.mark.asyncio
async def test_strict_chain_call_site_override_raises() -> None:
    """Per-call ``strict_chain=True`` overrides a lax store default."""
    revocations = RevocationStore(strict_chain=False)
    broken_fp = "b" * 64

    with pytest.raises(RuntimeError):
        await revocations.revoke_with_chain_event(
            audit_module=_BrokenAudit(),
            key_fingerprint=broken_fp,
            reason="per-call strict override",
            operator_id="on-call",
            strict_chain=True,  # explicit opt-in on a lax store
        )

    assert revocations.all() == []


@pytest.mark.asyncio
async def test_lax_mode_writes_row_with_distinct_warn_event() -> None:
    """Opt-in ``strict_chain=False`` preserves the v0.6.1 degraded path.

    The row is written with ``chain_event_id=None``, the runtime check
    honors it, AND a DISTINCT structured-log event
    (``identity.revocation_without_chain_event``) is emitted so SIEMs
    can alert on any revocation that lands without chain linkage.
    """
    revocations = RevocationStore(strict_chain=False)
    broken_fp = "c" * 64

    with structlog.testing.capture_logs() as captured:
        record = await revocations.revoke_with_chain_event(
            audit_module=_BrokenAudit(),
            key_fingerprint=broken_fp,
            reason="disaster-recovery window",
            operator_id="on-call",
        )

    assert record.chain_event_id is None
    # Runtime enforcement still works — the mirror row is there.
    assert revocations.is_revoked_at(broken_fp, chain_seq=0) is True

    # The distinct event name is what SIEM rules key on.
    event_names = [entry.get("event") for entry in captured]
    assert "identity.revocation_without_chain_event" in event_names
    # And the old generic name is NOT used — it was renamed in v0.6.2
    # so SIEM rules can tell "a revocation landed unlinked" from
    # "transient substrate hiccup that we recovered from".
    assert "identity.revocation_chain_event_failed" not in event_names

    # The WARN payload carries strict_chain=False so the reviewer can
    # tell this was a deliberate disaster-recovery override, not a
    # regression on the default.
    warn_entries = [
        e
        for e in captured
        if e.get("event") == "identity.revocation_without_chain_event"
    ]
    assert len(warn_entries) == 1
    assert warn_entries[0].get("log_level") == "warning"
    assert warn_entries[0].get("strict_chain") is False


@pytest.mark.asyncio
async def test_strict_mode_success_path_still_writes_linked_row() -> None:
    """Strict mode is ONLY a failure-path gate — the happy path still writes."""
    import secrets

    from codeatelier_governance.audit.module import AuditModule
    from codeatelier_governance.audit.store import InMemoryAuditStore
    from codeatelier_governance.identity.keystore import EphemeralKeyStore
    from codeatelier_governance.identity.signer import Ed25519Signer

    store = InMemoryAuditStore()
    keystore = EphemeralKeyStore(agent_id="governance.operator")
    signer = Ed25519Signer.from_private_key(keystore.load_private_key())
    audit = AuditModule(store, secret=secrets.token_bytes(32), signer=signer)
    await audit.start()
    try:
        revocations = RevocationStore()  # strict default
        fp = "d" * 64
        record = await revocations.revoke_with_chain_event(
            audit_module=audit,
            key_fingerprint=fp,
            reason="happy-path under strict default",
            operator_id="on-call",
        )
        assert record.chain_event_id is not None
        assert revocations.is_revoked_at(fp, chain_seq=0) is True
    finally:
        await audit.close()
