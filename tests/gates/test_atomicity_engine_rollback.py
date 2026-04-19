"""v0.6.2-followup — gates+audit atomicity characterization test.

The v0.6.2 ``on_commit`` fix closed the ORIGINAL ship-blocker (gate
resolved but audit raises → ghost resolution without audit row).  But
gates and audit run on SEPARATE engines with SEPARATE txns, so the
REVERSE failure mode — audit commits, gates rolls back at commit phase —
remains possible.  This file documents that residual window and asserts
its observable shape so operators know what to look for in production
chain inspections.

Window shape post-fix:

    audit engine txn COMMITs  ← audit row durable
    <crash / connection drop / pod SIGKILL>
    gates engine txn COMMIT fails at engine.begin().__aexit__
    ──────────────────────────────────────────────────────────
    residual: a GHOST ``approval.granted`` / ``approval.denied``
    audit row exists for a request_id whose gate STAYED pending.

Pre-fix window: BatchingWriter queue depth (up to 10k events) + 100ms
flush interval. Post-fix window: one DB round-trip. ~4 orders of
magnitude narrower but not zero. Single-engine transactional atomicity
would require merging audit and gates into one engine/schema — out of
scope for the v0.6.2 followup.

A retry with the same token after the crash WILL succeed (pending row
still there) and WILL emit a second audit row. Production chain audit
that finds two ``approval.granted`` events for one ``request_id``
should investigate the adjacent session for a failed commit event.
"""
from __future__ import annotations

import secrets as _secrets

import pytest

from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
from codeatelier_governance.audit.store import BatchingWriter
from codeatelier_governance.gates import GatesModule
from codeatelier_governance.gates.models import ApprovalRequest
from codeatelier_governance.gates.store import (
    InMemoryGatesStore,
    OnCommit,
    Resolution,
)


class _RollbackAtCommitStore(InMemoryGatesStore):
    """Simulates the PG path where ``engine.begin().__aexit__`` fails.

    Runs ``on_commit`` successfully (audit row gets durably written),
    then raises before committing the resolution state mutation. This
    models the exact window the v0.6.2-followup fix CANNOT close:
    audit engine commit succeeded, gates engine commit failed.

    InMemoryGatesStore's production behavior runs on_commit inside the
    asyncio lock then mutates the dicts. We intercept between those two
    steps. The raised exception propagates OUT, so the pending row
    remains pending — matching the post-rollback state PG would reach.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail_at_commit = False

    async def resolve(
        self,
        request_id,
        resolution: Resolution,
        *,
        on_commit: OnCommit | None = None,
    ) -> ApprovalRequest:
        from codeatelier_governance.gates.errors import ApprovalTokenError

        async with self._lock:
            if request_id in self._resolutions:
                raise ApprovalTokenError(
                    "approval token: already used (single-use only)"
                )
            req = self._pending.get(request_id)
            if req is None:
                raise ApprovalTokenError(
                    "approval token: unknown request_id"
                )
            # on_commit runs first — audit row becomes durable here.
            if on_commit is not None:
                await on_commit(req)
            # Now simulate engine.begin().__aexit__ failing. The audit
            # write already committed (separate engine); the gates-side
            # mutation below is skipped. Pending row stays pending.
            if self.fail_at_commit:
                raise ConnectionError(
                    "injected: gates engine COMMIT failed AFTER on_commit "
                    "returned (simulated connection drop at txn close)"
                )
            self._resolutions[request_id] = resolution
            self._resolved_requests[request_id] = req
            self._pending.pop(request_id, None)
            return req


@pytest.mark.asyncio
async def test_commit_phase_failure_after_on_commit_leaves_ghost_audit_row(
) -> None:
    """CHARACTERIZATION: documents the narrow residual window.

    Not a SHOULD-NOT-HAPPEN assertion — we ASSERT that the ghost row
    lands, because the current architecture cannot prevent it without
    merging audit and gates into one engine. If some future change
    makes this test FAIL (no ghost row), either atomicity was
    strengthened (great, re-write this test) or a regression swallowed
    the audit write at the wrong layer (investigate).
    """
    secret = _secrets.token_bytes(32)
    audit_store = InMemoryAuditStore(max_events=10_000)
    writer = BatchingWriter(
        primary=audit_store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    audit = AuditModule(audit_store, secret=secret, writer=writer)
    await audit.start()
    try:
        store = _RollbackAtCommitStore()
        gates = GatesModule(audit, secret=secret, store=store)

        req = await gates.request(
            "delete.patient", "agent-alpha", payload={"x": 1}
        )

        # Arm the commit-phase rollback. on_commit WILL run (and the
        # audit row will commit), then the store raises.
        store.fail_at_commit = True
        with pytest.raises(ConnectionError, match="gates engine COMMIT failed"):
            await gates.grant(req.token)

        await audit._writer.flush()

        # RESIDUAL RISK ASSERTION: the audit row IS durable even though
        # the gate stayed pending. This is what the pre-fix AND post-fix
        # code both exhibit for the reverse-failure direction — we
        # document it, we don't prevent it.
        granted = [
            e
            for e in audit_store._events.values()  # type: ignore[attr-defined]
            if e.kind == "approval.granted"
            and e.metadata.get("request_id") == str(req.request_id)
        ]
        assert len(granted) == 1, (
            "Expected the ghost audit row to be durable (audit engine "
            "committed before gates-engine rollback). If this assertion "
            "now fails with 0 rows, atomicity was strengthened — "
            "rewrite this characterization test."
        )

        # And the gate is still pending — operator can retry.
        assert (
            await gates._store.get_pending(req.request_id)  # type: ignore[attr-defined]
        ) is not None
        assert (
            await gates._store.get_resolution(req.request_id)  # type: ignore[attr-defined]
        ) is None

        # Retry path: disarm, grant again. A SECOND audit row lands —
        # two approval.granted for one request_id. Documented chain-
        # inspection signal for operators.
        store.fail_at_commit = False
        await gates.grant(req.token)
        await audit._writer.flush()

        granted_after = [
            e
            for e in audit_store._events.values()  # type: ignore[attr-defined]
            if e.kind == "approval.granted"
            and e.metadata.get("request_id") == str(req.request_id)
        ]
        assert len(granted_after) == 2, (
            "Post-retry we expect TWO audit rows for the same request_id "
            "— the ghost from the failed commit, plus the real one. This "
            "duplication is the chain-inspection signal operators use to "
            "detect the residual-window failure."
        )
    finally:
        await audit.close()
