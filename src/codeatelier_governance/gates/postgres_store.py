"""Postgres-backed HITL gates store.

Multi-process correct via single-table state machine:

    governance_gates_pending (
        request_id  UUID PK,
        ...
        resolved_at TIMESTAMPTZ NULL,
        resolution  VARCHAR(16) NULL  -- 'granted' / 'denied'
    )

``request()`` INSERTs a row with resolved_at NULL.
``grant()`` / ``deny()`` UPDATE SET resolved_at = NOW(), resolution = ?
WHERE request_id = ? AND resolved_at IS NULL — single-use is enforced
atomically by the database, regardless of which worker process executes
the call.
``wait_for()`` polls SELECT resolution every N seconds until it sees a
non-null value, the request expires, or the timeout elapses.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..utils import normalize_db_url
from .errors import ApprovalTokenError, GateError
from .models import ApprovalRequest
from .store import GatesStore, OnCommit, Resolution


class PostgresGatesStore(GatesStore):
    """SQLAlchemy/asyncpg-backed HITL gates store."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        if engine is not None:
            self._engine: AsyncEngine = engine
            self._owns_engine = False
        elif database_url is not None:
            self._engine = create_async_engine(
                normalize_db_url(database_url, component="gates store"),
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
            )
            self._owns_engine = True
        else:
            raise ValueError(
                "PostgresGatesStore requires either database_url or engine"
            )

    async def insert_pending(self, request: ApprovalRequest) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO governance_gates_pending
                            (request_id, agent_id, kind, action_hash,
                             token, expires_at, payload_json)
                        VALUES (:request_id, :agent_id, :kind, :action_hash,
                                :token, :expires_at, CAST(:payload AS JSONB))
                        """
                    ),
                    {
                        "request_id": str(request.request_id),
                        "agent_id": request.agent_id,
                        "kind": request.kind,
                        "action_hash": request.action_hash,
                        "token": request.token,
                        "expires_at": request.expires_at,
                        "payload": _safe_json(request.payload),
                    },
                )
        except Exception as exc:
            raise GateError(
                f"postgres gates.insert_pending failed: {type(exc).__name__}"
            ) from exc

    async def get_pending(
        self,
        request_id: UUID,
    ) -> ApprovalRequest | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT request_id, agent_id, kind, action_hash, "
                    "token, expires_at, payload_json "
                    "FROM governance_gates_pending "
                    "WHERE request_id = :rid AND resolved_at IS NULL"
                ),
                {"rid": str(request_id)},
            )
            row = res.mappings().first()
        if row is None:
            return None
        return ApprovalRequest(
            request_id=row["request_id"],
            agent_id=row["agent_id"],
            kind=row["kind"],
            action_hash=row["action_hash"],
            token=row["token"],
            expires_at=row["expires_at"],
            payload=row["payload_json"] or {},
        )

    async def resolve(
        self,
        request_id: UUID,
        resolution: Resolution,
        *,
        on_commit: OnCommit | None = None,
    ) -> ApprovalRequest:
        # Atomic UPDATE with single-use guard.
        #
        # v0.6.2 P0 atomicity: when ``on_commit`` is supplied, we run
        # it INSIDE the ``engine.begin()`` block after the UPDATE. If
        # ``on_commit`` raises, the async context manager rolls back
        # the UPDATE and the pending row is left in its pre-call state.
        # This is how we keep gate-resolved + audit-row atomic from
        # the caller's perspective: either both land, or neither does.
        #
        # Caveat: the audit write goes through a BatchingWriter backed
        # by a SEPARATE engine, so the audit INSERT is NOT literally in
        # this same Postgres transaction. It is still sufficient for
        # the threat model: the only failure mode we need to defend
        # against is "audit.log raises before the gate commits" — if
        # ``on_commit`` raises, our UPDATE rolls back; if it succeeds
        # (audit enqueued cleanly), the host crash risk now lives
        # entirely inside the audit writer's own durability guarantees
        # (which F6 hardened). Pre-v0.6.2 the gate UPDATE could commit
        # even when audit.log raised.
        row = None
        already_resolved_without_row = False
        try:
            async with self._engine.begin() as conn:
                res = await conn.execute(
                    text(
                        """
                        UPDATE governance_gates_pending
                        SET resolved_at = NOW(), resolution = :resolution
                        WHERE request_id = :rid AND resolved_at IS NULL
                        RETURNING request_id, agent_id, kind, action_hash,
                                  token, expires_at, payload_json
                        """
                    ),
                    {"rid": str(request_id), "resolution": resolution},
                )
                row = res.mappings().first()
                if row is None:
                    # Mark it so the surrounding try/except does not
                    # mis-classify this as a generic postgres error.
                    already_resolved_without_row = True
                    # Fall through — context manager will commit the
                    # no-op transaction and we raise below.
                elif on_commit is not None:
                    # Run the caller's commit hook inside the txn so
                    # any failure rolls back the UPDATE and leaves the
                    # pending row intact for retry. We re-wrap the row
                    # as an ApprovalRequest so the hook sees the stable
                    # public contract, not a SQLAlchemy RowMapping.
                    req = ApprovalRequest(
                        request_id=row["request_id"],
                        agent_id=row["agent_id"],
                        kind=row["kind"],
                        action_hash=row["action_hash"],
                        token=row["token"],
                        expires_at=row["expires_at"],
                        payload=row["payload_json"] or {},
                    )
                    await on_commit(req)
        except ApprovalTokenError:
            # The on_commit hook can raise ApprovalTokenError deliberately
            # (e.g. signalling an audit store contract violation). Let it
            # propagate untouched so callers get the real error, not the
            # generic "postgres gates.resolve failed" wrapper below.
            raise
        except Exception as exc:
            if already_resolved_without_row:
                # Will raise the right-shape error in the distinguish
                # block below; don't mask with the generic wrapper.
                row = None
            else:
                raise ApprovalTokenError(
                    f"postgres gates.resolve failed: {type(exc).__name__}"
                ) from exc

        if row is None:
            # Either unknown request_id or already resolved. Distinguish.
            async with self._engine.connect() as conn:
                check = await conn.execute(
                    text(
                        "SELECT resolved_at FROM governance_gates_pending "
                        "WHERE request_id = :rid"
                    ),
                    {"rid": str(request_id)},
                )
                check_row = check.first()
            if check_row is None:
                raise ApprovalTokenError(
                    "approval token: unknown request_id"
                )
            raise ApprovalTokenError(
                "approval token: already used (single-use only)"
            )
        return ApprovalRequest(
            request_id=row["request_id"],
            agent_id=row["agent_id"],
            kind=row["kind"],
            action_hash=row["action_hash"],
            token=row["token"],
            expires_at=row["expires_at"],
            payload=row["payload_json"] or {},
        )

    async def get_resolution(
        self,
        request_id: UUID,
    ) -> Resolution | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT resolution FROM governance_gates_pending "
                    "WHERE request_id = :rid"
                ),
                {"rid": str(request_id)},
            )
            row = res.first()
        if row is None or row[0] is None:
            return None
        val = str(row[0])
        if val == "granted":
            return "granted"
        if val == "denied":
            return "denied"
        return None

    async def has_granted_approval(
        self,
        agent_id: str,
    ) -> bool:
        """Return True if at least one unexpired granted approval exists for ``agent_id``.

        Fixed in v0.5.1 — prior versions returned ``False`` unconditionally
        via ContractsModule's private-attr fallback, which broke every
        HITL-gated contract deployed with a Postgres backend.
        """
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT 1 FROM governance_gates_pending "
                    "WHERE agent_id = :agent_id "
                    "  AND resolution = 'granted' "
                    "  AND expires_at > NOW() "
                    "LIMIT 1"
                ),
                {"agent_id": agent_id},
            )
            row = res.first()
        return row is not None

    async def cleanup_resolved(
        self,
        older_than_days: int = 90,
    ) -> int:
        """Delete resolved gate rows older than ``older_than_days`` days.

        Operators should run this periodically (cron, scheduled job, manual)
        to keep the table from growing unbounded. v0.1.5 does not auto-run
        this; the operator chooses the cadence and retention window.

        Returns the number of rows deleted.

        IMPORTANT: only deletes rows where ``resolved_at IS NOT NULL``.
        Pending requests are NEVER deleted by this method.
        """
        if older_than_days < 0:
            raise ValueError("older_than_days must be non-negative")
        async with self._engine.begin() as conn:
            res = await conn.execute(
                text(
                    "DELETE FROM governance_gates_pending "
                    "WHERE resolved_at IS NOT NULL "
                    "  AND resolved_at < NOW() - make_interval(days => :days)"
                ),
                {"days": older_than_days},
            )
        return res.rowcount or 0

    async def close(self) -> None:
        """Dispose the engine only if this store owns it."""
        if self._owns_engine:
            await self._engine.dispose()


def _safe_json(value: object) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return "{}"
