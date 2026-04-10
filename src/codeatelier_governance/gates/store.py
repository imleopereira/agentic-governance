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
from typing import Literal
from uuid import UUID

from .errors import ApprovalTokenError
from .models import ApprovalRequest

Resolution = Literal["granted", "denied"]


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
    ) -> ApprovalRequest:
        """Atomically resolve a pending request as granted or denied.

        Single-use semantics enforced at the database via
        ``WHERE resolved_at IS NULL``. Raises if already resolved.
        """

    @abstractmethod
    async def get_resolution(
        self,
        request_id: UUID,
    ) -> Resolution | None:
        """Return ``'granted'`` / ``'denied'`` / None (still pending)."""

    async def close(self) -> None:
        return None


class InMemoryGatesStore(GatesStore):
    """In-memory gates store. Single-process only."""

    def __init__(self) -> None:
        self._pending: dict[UUID, ApprovalRequest] = {}
        self._resolutions: dict[UUID, Resolution] = {}
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
            self._resolutions[request_id] = resolution
            self._pending.pop(request_id, None)
            return req

    async def get_resolution(
        self,
        request_id: UUID,
    ) -> Resolution | None:
        async with self._lock:
            return self._resolutions.get(request_id)
