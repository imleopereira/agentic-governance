"""v0.6.2 P0 — ``GatesModule._resolve`` must be atomic with audit.log.

Pre-v0.6.2 the resolve path in :class:`GatesModule` executed two
independent async round-trips:

    1. ``await self._store.resolve(...)``   ← DB commit A
    2. ``await self._audit.log(...)``       ← separate DB round-trip B

If the worker crashed between A and B (OOM, pod eviction, SIGKILL),
the gate row was marked resolved in Postgres but no ``approval.granted``
/ ``approval.denied`` audit event ever landed in the HMAC chain. The
Article 12 export would then silently omit the human decision that
released the action — exactly the failure mode Article 12 ``LOGS``
exists to prevent.

Anti-pattern called out in ``.agents/swe.md``:
    "'Check + write audit' pairs outside a single transaction".

v0.6.2 fixes this by passing the audit-emit as an ``on_commit`` hook
into the store. The store runs the hook INSIDE its atomic unit (the
asyncio lock for :class:`InMemoryGatesStore`, the ``engine.begin()``
txn for :class:`PostgresGatesStore`) BEFORE persisting the resolution.
If the hook raises, the resolution does NOT become visible and the
pending row stays intact so the operator can retry with the same
token.

This test exercises the in-memory store path end-to-end with a real
AuditModule that is wired to a store which REFUSES the write. That
way the atomicity check is on real code, not a mocked-out audit
layer (which would hide the bug — see the LLM-theater warning in the
spec).
"""
from __future__ import annotations

import secrets as _secrets
from datetime import timedelta

import pytest

from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.audit.store import BatchingWriter
from codeatelier_governance.gates import ApprovalTokenError, GatesModule


class _RaisingAuditModule(AuditModule):
    """AuditModule whose ``log`` raises AFTER the normal log path
    would have committed.

    Subclassing :class:`AuditModule` rather than mocking it keeps the
    real chain-construction code path around the failure injection —
    a flat ``AsyncMock(side_effect=RuntimeError)`` would let a buggy
    ``GatesModule._resolve`` still pass the test because the mock
    never exercised the real write pipeline.
    """

    fail_next: bool = False

    async def log(self, event: AuditEvent):  # type: ignore[override]
        if self.fail_next:
            # Simulate the worker-dies-mid-resolve hazard: audit.log
            # starts, the process OOMs before it can return. The
            # visible effect to the caller is an exception propagating
            # out of the log call.
            raise RuntimeError(
                "injected: audit writer died mid-call (simulated OOM)"
            )
        return await super().log(event)


@pytest.fixture
async def raising_audit() -> _RaisingAuditModule:
    audit_store = InMemoryAuditStore(max_events=10_000)
    writer = BatchingWriter(
        primary=audit_store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    secret = _secrets.token_bytes(32)
    module = _RaisingAuditModule(audit_store, secret=secret, writer=writer)
    await module.start()
    try:
        yield module
    finally:
        await module.close()


@pytest.mark.asyncio
async def test_resolve_rolls_back_when_audit_log_raises(
    raising_audit: _RaisingAuditModule,
) -> None:
    """Audit failure AFTER the resolve path enters → pending stays.

    Pre-fix: ``store.resolve()`` committed first and ``audit.log()``
    raised second — the gate was resolved with no audit row.

    Post-fix: audit runs inside the store's atomic unit, so a raise
    rolls back the mutation. Observable invariant: the request is
    still pending, a second grant with the SAME token succeeds once
    the audit layer is repaired, and exactly ONE audit row lands.
    """
    secret = _secrets.token_bytes(32)
    gates = GatesModule(raising_audit, secret=secret)

    req = await gates.request("delete.patient", "agent-alpha", payload={})

    # Arm the failure and attempt the grant. The raise must propagate
    # — this is the outward-visible symptom that the resolve did NOT
    # silently commit.
    raising_audit.fail_next = True
    with pytest.raises(RuntimeError, match="audit writer died"):
        await gates.grant(req.token)

    # Key atomicity invariant: the pending row is still pending. If
    # the pre-v0.6.2 ordering were still in place, ``get_pending``
    # would return None because the pending entry had been popped
    # inside ``InMemoryGatesStore.resolve``.
    still_pending = await gates._store.get_pending(req.request_id)  # type: ignore[attr-defined]
    assert still_pending is not None, (
        "Pending gate row was consumed despite audit.log failure — "
        "resolve path is not atomic with audit write (v0.6.2 P0)."
    )

    # Confirm no resolution is visible yet.
    assert (
        await gates._store.get_resolution(req.request_id)  # type: ignore[attr-defined]
    ) is None

    # No audit events landed either.
    await raising_audit._writer.flush()
    granted = [
        e
        for e in raising_audit._store._events.values()  # type: ignore[attr-defined]
        if e.kind == "approval.granted"
    ]
    assert granted == []

    # Operator repairs the audit layer; retry the SAME token.
    raising_audit.fail_next = False
    await gates.grant(req.token)
    await raising_audit._writer.flush()

    # Now exactly one audit row exists and the resolution is visible.
    granted = [
        e
        for e in raising_audit._store._events.values()  # type: ignore[attr-defined]
        if e.kind == "approval.granted"
    ]
    assert len(granted) == 1
    assert granted[0].metadata["request_id"] == str(req.request_id)
    assert (
        await gates._store.get_resolution(req.request_id)  # type: ignore[attr-defined]
    ) == "granted"


@pytest.mark.asyncio
async def test_deny_rolls_back_when_audit_log_raises(
    raising_audit: _RaisingAuditModule,
) -> None:
    """Same atomicity contract applies to deny, not just grant."""
    secret = _secrets.token_bytes(32)
    gates = GatesModule(raising_audit, secret=secret)

    req = await gates.request("delete.patient", "agent-alpha", payload={})

    raising_audit.fail_next = True
    with pytest.raises(RuntimeError, match="audit writer died"):
        await gates.deny(req.token)

    still_pending = await gates._store.get_pending(req.request_id)  # type: ignore[attr-defined]
    assert still_pending is not None

    # Retry works — no single-use blowup since we never committed.
    raising_audit.fail_next = False
    await gates.deny(req.token)
    await raising_audit._writer.flush()

    denied = [
        e
        for e in raising_audit._store._events.values()  # type: ignore[attr-defined]
        if e.kind == "approval.denied"
    ]
    assert len(denied) == 1


@pytest.mark.asyncio
async def test_single_use_guard_still_holds_after_successful_resolve(
    raising_audit: _RaisingAuditModule,
) -> None:
    """A successful resolve is still single-use.

    The atomicity fix MUST NOT weaken the existing replay defense.
    After a clean grant, a second grant with the same token raises
    ``already used``, just like pre-v0.6.2.
    """
    secret = _secrets.token_bytes(32)
    gates = GatesModule(raising_audit, secret=secret)

    req = await gates.request("delete.patient", "agent-alpha", payload={})

    # Clean resolve, no audit failure.
    raising_audit.fail_next = False
    await gates.grant(req.token)

    with pytest.raises(ApprovalTokenError, match="already used"):
        await gates.grant(req.token)


@pytest.mark.asyncio
async def test_action_hash_mismatch_aborts_before_audit_write(
    raising_audit: _RaisingAuditModule,
) -> None:
    """action_hash mismatch → raise BEFORE any audit row is written.

    Defense in depth: the action_hash cross-check in ``_resolve`` runs
    before ``store.resolve`` is called, so a bogus token must not
    even reach the atomic unit. Arming ``fail_next`` proves the audit
    layer is not invoked — if it were, we'd see the RuntimeError, not
    the ``action_hash mismatch`` error.
    """
    from datetime import datetime, timezone

    from codeatelier_governance.gates.tokens import make_token

    secret = _secrets.token_bytes(32)
    gates = GatesModule(raising_audit, secret=secret)

    req = await gates.request("delete.patient", "agent-alpha", payload={"x": 1})
    # Forge a token with the SAME request_id but a DIFFERENT action_hash.
    bad_token = make_token(
        secret=secret,
        request_id=req.request_id,
        action_hash="0" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    raising_audit.fail_next = True  # Should not be reached.
    with pytest.raises(ApprovalTokenError, match="action_hash mismatch"):
        await gates.grant(bad_token)

    # Pending row untouched.
    assert (
        await gates._store.get_pending(req.request_id)  # type: ignore[attr-defined]
    ) is not None
