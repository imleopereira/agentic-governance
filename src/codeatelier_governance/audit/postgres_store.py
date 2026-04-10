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

from sqlalchemy import Column, DateTime, MetaData, String, Table, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .errors import StoreUnavailableError
from .models import AuditEventRecord
from .store import AuditStore, ChainBuilder


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
    Column("input_hash", String(128), nullable=True),
    Column("output_hash", String(128), nullable=True),
    Column("metadata_json", JSONB, nullable=False),
    Column("prev_hash", String(128), nullable=True),
    Column("hmac_value", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
)


def _normalize_url(url: str) -> str:
    """Convert plain postgresql:// URLs to the async asyncpg dialect."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    raise ValueError(
        "audit store: expected a postgresql:// connection string. "
        "Fix: pass database_url='postgresql://user:pass@host/db'"
    )


class PostgresAuditStore(AuditStore):
    """SQLAlchemy/asyncpg-backed AuditStore."""

    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            _normalize_url(database_url),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
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
                            "input_hash": record.input_hash,
                            "output_hash": record.output_hash,
                            "metadata_json": record.metadata,
                            "prev_hash": record.prev_hash,
                            "hmac_value": record.hmac,
                            "created_at": record.created_at,
                        }
                    ],
                )
            return record
        except Exception as exc:
            raise StoreUnavailableError(
                f"postgres insert_with_chain_lock failed: {type(exc).__name__}"
            ) from exc

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
                "input_hash": e.input_hash,
                "output_hash": e.output_hash,
                "metadata_json": e.metadata,
                "prev_hash": e.prev_hash,
                "hmac_value": e.hmac,
                "created_at": e.created_at,
            }
            for e in events
        ]
        try:
            async with self._engine.begin() as conn:
                await conn.execute(audit_events.insert(), rows)
        except Exception as exc:
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
                    "kind, input_hash, output_hash, metadata_json, "
                    "prev_hash, hmac_value, created_at "
                    "FROM governance_audit_events "
                    "WHERE session_id = :sid "
                    "ORDER BY chain_seq"
                ),
                {"sid": str(session_id)},
            )
            rows = list(res.mappings())
        return [_row_to_record(row) for row in rows]

    async def close(self) -> None:
        await self._engine.dispose()


def _row_to_record(row: Any) -> AuditEventRecord:
    return AuditEventRecord(
        event_id=row["event_id"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        parent_event_id=row["parent_event_id"],
        kind=row["kind"],
        input_hash=row["input_hash"],
        output_hash=row["output_hash"],
        metadata=row["metadata_json"],
        prev_hash=row["prev_hash"],
        hmac=row["hmac_value"],
        created_at=row["created_at"],
    )
