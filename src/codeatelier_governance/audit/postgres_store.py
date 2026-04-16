"""Postgres-backed AuditStore using SQLAlchemy 2.0 + asyncpg.

The schema is defined here in code AND in ddl.sql; alembic generates
migrations from the SQLAlchemy table later. ddl.sql also installs the
append-only triggers — those are NOT representable in SQLAlchemy and must
be applied separately as part of the migration.

Operators MUST also REVOKE UPDATE, DELETE, TRUNCATE on this table from the
SDK's database role for defense-in-depth. See ddl.sql.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import struct

from sqlalchemy import (
    Column,
    DateTime,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import structlog

from ..utils import normalize_db_url
from .errors import StoreUnavailableError
from .models import AuditEventRecord
from .store import AuditStore, ChainBuilder

_logger = structlog.get_logger(__name__)

# BLOCKER DX: v0.6 adds these columns to governance_audit_events. If the
# schema is missing any of them, the very first write blows up with an
# opaque ProgrammingError. We name them here so both the startup self-
# check and the per-write catch handler can reference the same set.
_REQUIRED_V06_COLUMNS = ("signature", "signing_key_fingerprint", "signature_status")


def _is_missing_v06_columns_error(exc: BaseException) -> bool:
    """True if the exception looks like 'a v0.6 column does not exist'.

    Matches both the asyncpg UndefinedColumnError and the SQLAlchemy
    ProgrammingError wrapper. The match is on substrings of the error
    message because we cannot import asyncpg.exceptions safely from
    every install profile.
    """
    msg = str(exc).lower()
    if "does not exist" not in msg and "undefined column" not in msg:
        return False
    return any(col in msg for col in _REQUIRED_V06_COLUMNS)


def _v06_alembic_message() -> str:
    """Operator-actionable diagnostic for missing v0.6 columns."""
    return (
        "governance_audit_events is missing v0.6 columns "
        f"({', '.join(_REQUIRED_V06_COLUMNS)}). Run "
        "'alembic upgrade head' to apply the v0.6 migrations. "
        "See docs/migrations.md."
    )


def _session_lock_key(session_id: UUID) -> int:
    """Convert a session UUID to a signed 64-bit int for advisory locking.

    pg_advisory_xact_lock(bigint) takes a 64-bit signed integer. Using
    hashtext() of the UUID string would only give us 32 bits and trigger
    birthday-paradox collisions at ~65k unique sessions. Instead we read
    the first 8 bytes of the UUID directly — UUID v4 is random across
    those bytes, so collisions only occur at ~4.3 billion sessions.
    """
    value: int = struct.unpack(">q", session_id.bytes[:8])[0]
    return value

metadata = MetaData()

audit_events = Table(
    "governance_audit_events",
    metadata,
    Column("event_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("session_id", PG_UUID(as_uuid=True), nullable=False, index=True),
    Column("agent_id", String(256), nullable=False, index=True),
    Column("parent_event_id", PG_UUID(as_uuid=True), nullable=True, index=True),
    Column("kind", String(128), nullable=False),
    Column("model", String(128), nullable=True),
    Column("input_hash", String(128), nullable=True),
    Column("output_hash", String(128), nullable=True),
    Column("metadata_json", JSONB, nullable=False),
    Column("prev_hash", String(128), nullable=True),
    Column("hmac_value", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    # F6 Track A: Ed25519 agent identity columns (migration f6a1ed25519aid).
    Column("signature", LargeBinary(), nullable=True),
    Column("signing_key_fingerprint", Text(), nullable=True),
    Column("signature_status", Text(), nullable=False, server_default="unsigned"),
)


class PostgresAuditStore(AuditStore):
    """SQLAlchemy/asyncpg-backed AuditStore."""

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
                normalize_db_url(database_url, component="audit store"),
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
            )
            self._owns_engine = True
        else:
            raise ValueError(
                "PostgresAuditStore requires either database_url or engine"
            )

    async def start(self) -> None:
        """BLOCKER DX: opportunistic startup schema check.

        Queries information_schema for the v0.6 audit columns. Logs a
        loud structlog WARNING naming the missing columns when one or
        more are absent, so an operator sees the mismatch BEFORE the
        first customer request hits and tries to write. Never raises —
        a startup that cannot reach the DB yet must still come up
        (host application MUST continue working if the governance DB
        is unreachable, per CLAUDE.md invariant #1).
        """
        try:
            async with self._engine.connect() as conn:
                res = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'governance_audit_events' "
                        "AND column_name IN ('signature', 'signing_key_fingerprint', "
                        "'signature_status')"
                    )
                )
                present = {r[0] for r in res}
        except Exception as exc:  # noqa: BLE001 — defensive, see docstring
            _logger.warning(
                "audit.postgres_store.startup_check_failed",
                error_type=type(exc).__name__,
            )
            return
        missing = [c for c in _REQUIRED_V06_COLUMNS if c not in present]
        if missing:
            _logger.warning(
                "audit.postgres_store.missing_v0_6_columns",
                missing_columns=missing,
                remediation=(
                    "Run 'alembic upgrade head' to apply the v0.6 migrations."
                ),
            )

    async def insert_with_chain_lock(
        self,
        session_id: UUID,
        builder: ChainBuilder,
    ) -> AuditEventRecord:
        """Atomic chain construction across all workers via Postgres advisory locks.

        Optimistic two-phase pattern:
            1. Read prev_hash WITHOUT holding the lock (snapshot read).
            2. Call ``builder(prev_hash)`` outside the lock to compute the
               record (HMAC computation runs concurrently across workers).
            3. Open a transaction, acquire the per-session advisory lock,
               re-read prev_hash to verify it hasn't changed under us.
            4. If unchanged: INSERT and commit (releases the lock).
            5. If changed (rare race): rebuild the record with the actual
               prev_hash, then INSERT.

        The lock-held window is reduced to roughly two SELECTs + an INSERT
        — no application-level computation runs while the lock is held —
        so contention drops sharply under high session-internal concurrency.
        """
        lock_key = _session_lock_key(session_id)
        try:
            # Phase 1: optimistic read (no lock held)
            optimistic_prev = await self._read_last_hmac_no_lock(session_id)
            # Phase 1.5: build the record with the optimistic prev_hash
            record = await builder(optimistic_prev)

            # Phase 2: lock + verify + insert
            async with self._engine.begin() as conn:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": lock_key},
                )
                res = await conn.execute(
                    text(
                        "SELECT hmac_value FROM governance_audit_events "
                        "WHERE session_id = :sid "
                        "ORDER BY chain_seq DESC LIMIT 1"
                    ),
                    {"sid": str(session_id)},
                )
                row = res.first()
                actual_prev: str | None = row[0] if row is not None else None
                if actual_prev != optimistic_prev:
                    # Race: another worker inserted while we were computing.
                    # Rebuild the record with the real prev_hash.
                    record = await builder(actual_prev)
                await conn.execute(
                    audit_events.insert(),
                    [
                        {
                            "event_id": record.event_id,
                            "session_id": record.session_id,
                            "agent_id": record.agent_id,
                            "parent_event_id": record.parent_event_id,
                            "kind": record.kind,
                            "model": record.model,
                            "input_hash": record.input_hash,
                            "output_hash": record.output_hash,
                            "metadata_json": record.metadata,
                            "prev_hash": record.prev_hash,
                            "hmac_value": record.hmac,
                            "created_at": record.created_at,
                            "signature": record.signature,
                            "signing_key_fingerprint": record.signing_key_fingerprint,
                            "signature_status": record.signature_status,
                        }
                    ],
                )
            return record
        except Exception as exc:
            # BLOCKER DX: name the cause when the schema is pre-v0.6.
            if _is_missing_v06_columns_error(exc):
                raise StoreUnavailableError(_v06_alembic_message()) from exc
            raise StoreUnavailableError(
                f"postgres insert_with_chain_lock failed: {type(exc).__name__}"
            ) from exc

    async def get_current_chain_seq(self) -> int:
        """Return ``MAX(chain_seq)`` across all sessions, or 0 on empty chain.

        Used by the F6 Track A key registration path to stamp
        ``activated_at_chain_seq`` with the current chain head rather than
        a hardcoded 0. If the table is unreachable the caller is expected
        to catch the exception and treat the registration as deferred.
        """
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text("SELECT COALESCE(MAX(chain_seq), 0) FROM governance_audit_events")
            )
            row = res.first()
        return int(row[0]) if row is not None and row[0] is not None else 0

    async def _read_last_hmac_no_lock(self, session_id: UUID) -> str | None:
        """Optimistic read of the latest hmac for a session, no advisory lock."""
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT hmac_value FROM governance_audit_events "
                    "WHERE session_id = :sid "
                    "ORDER BY chain_seq DESC LIMIT 1"
                ),
                {"sid": str(session_id)},
            )
            row = res.first()
        return row[0] if row is not None else None

    async def write_batch(self, events: list[AuditEventRecord]) -> None:
        if not events:
            return
        rows = [
            {
                "event_id": e.event_id,
                "session_id": e.session_id,
                "agent_id": e.agent_id,
                "parent_event_id": e.parent_event_id,
                "kind": e.kind,
                "model": e.model,
                "input_hash": e.input_hash,
                "output_hash": e.output_hash,
                "metadata_json": e.metadata,
                "prev_hash": e.prev_hash,
                "hmac_value": e.hmac,
                "created_at": e.created_at,
                "signature": e.signature,
                "signing_key_fingerprint": e.signing_key_fingerprint,
                "signature_status": e.signature_status,
            }
            for e in events
        ]
        try:
            async with self._engine.begin() as conn:
                await conn.execute(audit_events.insert(), rows)
        except Exception as exc:
            # BLOCKER DX: name the cause when the schema is pre-v0.6.
            if _is_missing_v06_columns_error(exc):
                raise StoreUnavailableError(_v06_alembic_message()) from exc
            # Never leak the underlying error message — it may contain DB URLs.
            raise StoreUnavailableError(
                f"postgres write failed: {type(exc).__name__}"
            ) from exc

    async def get_event(self, event_id: UUID) -> AuditEventRecord | None:
        stmt = select(audit_events).where(audit_events.c.event_id == event_id)
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            row = result.mappings().first()
        return _row_to_record(row) if row is not None else None

    async def get_chain(self, event_id: UUID) -> list[AuditEventRecord]:
        chain: list[AuditEventRecord] = []
        current: UUID | None = event_id
        visited: set[UUID] = set()
        while current is not None:
            if current in visited:
                break
            visited.add(current)
            record = await self.get_event(current)
            if record is None:
                break
            chain.append(record)
            current = record.parent_event_id
        return list(reversed(chain))

    async def get_last_hmac(self, session_id: UUID) -> str | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT hmac_value FROM governance_audit_events "
                    "WHERE session_id = :sid "
                    "ORDER BY chain_seq DESC LIMIT 1"
                ),
                {"sid": str(session_id)},
            )
            row = res.first()
        return row[0] if row is not None else None

    async def get_session_events(
        self, session_id: UUID
    ) -> list[AuditEventRecord]:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT event_id, session_id, agent_id, parent_event_id, "
                    "kind, model, input_hash, output_hash, metadata_json, "
                    "prev_hash, hmac_value, created_at, "
                    "signature, signing_key_fingerprint, signature_status "
                    "FROM governance_audit_events "
                    "WHERE session_id = :sid "
                    "ORDER BY chain_seq"
                ),
                {"sid": str(session_id)},
            )
            rows = list(res.mappings())
        return [_row_to_record(row) for row in rows]

    async def close(self) -> None:
        """Dispose the engine only if this store owns it."""
        if self._owns_engine:
            await self._engine.dispose()


def _row_to_record(row: Any) -> AuditEventRecord:
    return AuditEventRecord(
        event_id=row["event_id"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        parent_event_id=row["parent_event_id"],
        kind=row["kind"],
        model=row["model"],
        input_hash=row["input_hash"],
        output_hash=row["output_hash"],
        metadata=row["metadata_json"],
        prev_hash=row["prev_hash"],
        hmac=row["hmac_value"],
        created_at=row["created_at"],
        signature=row.get("signature"),
        signing_key_fingerprint=row.get("signing_key_fingerprint"),
        signature_status=row.get("signature_status") or "unsigned",
    )
