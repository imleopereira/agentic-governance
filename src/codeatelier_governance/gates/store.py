"""Gates store abstraction + in-memory implementation.

Two implementations:
    InMemoryGatesStore — single-process; survives only the current process
    PostgresGatesStore — multi-process correct (see postgres_store.py)

Pending requests are persisted; ``grant``/``deny`` UPDATE the row with a
``WHERE resolved_at IS NULL`` clause to enforce single-use atomicity at
the database. ``wait_for`` polls the row state — polling is the source of
truth, LISTEN/NOTIFY is an optimization (not implemented in v0.1.5).
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Literal
from uuid import UUID

from .errors import ApprovalTokenError
from .models import ApprovalRequest

Resolution = Literal["granted", "denied"]

# v0.6.2 P0 — atomic resolve + audit.
#
# ``on_commit`` is an async hook invoked by ``resolve`` AFTER the
# single-use / unknown-request_id checks have passed but BEFORE the
# resolution is made visible (in-memory mutation committed, DB txn
# committed). It receives the pending :class:`ApprovalRequest` and may
# raise. If it raises, the resolution MUST NOT become visible: the
# in-memory store does not mutate its dicts, the postgres store rolls
# back the UPDATE via its transaction context. Either way the pending
# row is left intact so a retry can succeed.
#
# This closes the pre-v0.6.2 hole where ``_resolve`` did
# ``store.resolve()`` (committed) and THEN ``audit.log()`` (separate
# round-trip). If the worker died between the two, the gate was
# resolved with no audit row — Article 12 export silently missing the
# decision.
OnCommit = Callable[[ApprovalRequest], Awaitable[None]]


class GatesStore(ABC):
    """Abstract HITL gates storage backend."""

    @abstractmethod
    async def insert_pending(self, request: ApprovalRequest) -> None:
        """Persist a new pending approval request."""

    @abstractmethod
    async def get_pending(
        self,
        request_id: UUID,
    ) -> ApprovalRequest | None:
        """Fetch a pending request, or None if missing or already resolved."""

    @abstractmethod
    async def resolve(
        self,
        request_id: UUID,
        resolution: Resolution,
        *,
        on_commit: OnCommit | None = None,
    ) -> ApprovalRequest:
        """Atomically resolve a pending request as granted or denied.

        Single-use semantics enforced at the database via
        ``WHERE resolved_at IS NULL``. Raises if already resolved.

        When ``on_commit`` is provided, it runs inside the same atomic
        unit as the state mutation: for :class:`InMemoryGatesStore` that
        means inside the per-store asyncio lock AND before the resolved
        dicts are mutated; for :class:`PostgresGatesStore` that means
        inside the same ``engine.begin()`` transaction that runs the
        single-use UPDATE. If ``on_commit`` raises, the resolution is
        NOT persisted and the pending row remains intact. See the
        module-level ``OnCommit`` docstring for the threat model.
        """

    @abstractmethod
    async def get_resolution(
        self,
        request_id: UUID,
    ) -> Resolution | None:
        """Return ``'granted'`` / ``'denied'`` / None (still pending)."""

    @abstractmethod
    async def has_granted_approval(
        self,
        agent_id: str,
    ) -> bool:
        """Return True if at least one unexpired granted approval exists for ``agent_id``.

        Used by ``ContractsModule._check_hitl_approved`` to answer
        "can this agent proceed with an HITL-gated action?".  The check
        is scoped to the agent_id and must exclude expired approvals.

        Added in v0.5.1.  Prior to v0.5.1 the check was implemented by
        reaching into ``InMemoryGatesStore`` private attributes, and
        returned ``False`` unconditionally for ``PostgresGatesStore`` —
        which broke HITL-gated contracts in every production deployment
        using a Postgres backend (over-blocking: legitimate approved
        actions were denied).
        """

    async def close(self) -> None:
        return None


class InMemoryGatesStore(GatesStore):
    """In-memory gates store. Single-process only."""

    def __init__(self) -> None:
        self._pending: dict[UUID, ApprovalRequest] = {}
        self._resolutions: dict[UUID, Resolution] = {}
        # v0.5.1: retain the resolved request record (not just the
        # resolution literal) so ``has_granted_approval`` can filter by
        # agent_id and expires_at.  Prior to v0.5.1 the resolve() path
        # popped the pending entry, losing agent_id.
        self._resolved_requests: dict[UUID, ApprovalRequest] = {}
        self._lock = asyncio.Lock()

    async def insert_pending(self, request: ApprovalRequest) -> None:
        async with self._lock:
            self._pending[request.request_id] = request

    async def get_pending(
        self,
        request_id: UUID,
    ) -> ApprovalRequest | None:
        async with self._lock:
            return self._pending.get(request_id)

    async def resolve(
        self,
        request_id: UUID,
        resolution: Resolution,
        *,
        on_commit: OnCommit | None = None,
    ) -> ApprovalRequest:
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
            # v0.6.2 P0 atomicity: run the audit-emit hook BEFORE mutating
            # resolution state. If it raises, the pending row stays in
            # ``self._pending`` and the resolution dicts are not written,
            # so a retry (same token, after operator fixes the audit
            # layer) can succeed. Matches the txn-rollback behaviour of
            # the postgres backend.
            if on_commit is not None:
                await on_commit(req)
            self._resolutions[request_id] = resolution
            self._resolved_requests[request_id] = req
            self._pending.pop(request_id, None)
            return req

    async def get_resolution(
        self,
        request_id: UUID,
    ) -> Resolution | None:
        async with self._lock:
            return self._resolutions.get(request_id)

    async def has_granted_approval(
        self,
        agent_id: str,
    ) -> bool:
        """Return True if an unexpired granted approval exists for ``agent_id``."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        async with self._lock:
            for req_id, resolution in self._resolutions.items():
                if resolution != "granted":
                    continue
                req = self._resolved_requests.get(req_id)
                if req is None:
                    continue
                if req.agent_id != agent_id:
                    continue
                expires_at = req.expires_at
                # Normalise tz-naive expires_at to UTC so comparison does
                # not raise TypeError.
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= now:
                    continue
                return True
        return False
