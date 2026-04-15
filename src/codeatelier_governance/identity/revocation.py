"""Revocation store for F6 Ed25519 agent keys (design constraint #4).

Append-only. A revocation row is identified by
``(key_fingerprint, revoked_at_chain_seq)`` and carries a ``reason`` and
``operator_id``. The verifier rejects any row signed by ``key_fingerprint``
whose ``chain_seq >= revoked_at_chain_seq``.

Two construction modes (same pattern as ``AgentKeyRegistry``):

* ``RevocationStore()`` — in-memory, zero-config; used by unit tests.
* ``RevocationStore(engine=<AsyncEngine>)`` — SQLAlchemy-backed against
  ``governance_agent_key_revocations`` (migration ``f6a1ed25519aid``).

**Append-only at the application level**: there is no ``update`` or
``delete`` method — only ``revoke`` (append) and read-only queries. The DB
mirrors this via ``REVOKE UPDATE, DELETE`` grants in the migration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class RevocationRecord:
    """Mirrors ``governance_agent_key_revocations``. Append-only."""

    key_fingerprint: str
    revoked_at_chain_seq: int
    reason: str
    operator_id: str
    created_at: datetime


class RevocationStore:
    """Revocation log — in-memory or SQLAlchemy-backed.

    Two construction modes: zero-config in-memory (default; tests and
    ephemeral SDK mode) or Postgres-backed via an ``AsyncEngine``.

    **No public mutation API beyond** ``revoke``. Any code attempting to
    ``update`` or ``delete`` a revocation row is a policy violation and is
    rejected at the API level (the methods simply do not exist).
    """

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine
        self._records: list[RevocationRecord] = []
        self._by_fingerprint: dict[str, list[RevocationRecord]] = {}
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Legacy sync API — used by existing unit tests.
    # ------------------------------------------------------------------

    def revoke(
        self,
        *,
        key_fingerprint: str,
        revoked_at_chain_seq: int,
        reason: str,
        operator_id: str,
    ) -> RevocationRecord:
        """Append a revocation row (in-memory mirror)."""
        record = RevocationRecord(
            key_fingerprint=key_fingerprint,
            revoked_at_chain_seq=revoked_at_chain_seq,
            reason=reason,
            operator_id=operator_id,
            created_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._records.append(record)
            self._by_fingerprint.setdefault(key_fingerprint, []).append(record)
        return record

    def is_revoked_at(self, key_fingerprint: str, chain_seq: int) -> bool:
        """Sync variant: forward-looking revocation check against mirror.

        A row with ``chain_seq == revoked_at_chain_seq`` IS considered
        revoked — the revocation fires at that seq, not after.
        """
        for rec in self._by_fingerprint.get(key_fingerprint, []):
            if rec.revoked_at_chain_seq <= chain_seq:
                return True
        return False

    def all(self) -> list[RevocationRecord]:
        return list(self._records)

    # ------------------------------------------------------------------
    # Async SQLAlchemy API — requires ``engine`` at construction time.
    # ------------------------------------------------------------------

    async def revoke_async(
        self,
        fingerprint: str,
        revoked_at_chain_seq: int,
        reason: str,
        operator_id: str,
    ) -> RevocationRecord:
        """Append a revocation row to Postgres AND the in-memory mirror."""
        if self._engine is None:
            raise RuntimeError(
                "revoke_async requires an AsyncEngine — instantiate with "
                "RevocationStore(engine=...)."
            )
        from sqlalchemy import text

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO governance_agent_key_revocations "
                    "(key_fingerprint, revoked_at_chain_seq, reason, "
                    "operator_id) VALUES (:fp, :seq, :reason, :op) "
                    "ON CONFLICT (key_fingerprint, revoked_at_chain_seq) "
                    "DO NOTHING"
                ),
                {
                    "fp": fingerprint,
                    "seq": revoked_at_chain_seq,
                    "reason": reason,
                    "op": operator_id,
                },
            )
        return self.revoke(
            key_fingerprint=fingerprint,
            revoked_at_chain_seq=revoked_at_chain_seq,
            reason=reason,
            operator_id=operator_id,
        )

    async def is_revoked_at_seq(
        self, fingerprint: str, chain_seq: int
    ) -> bool:
        """Async variant used by the production verifier path.

        Consults the in-memory mirror first, then the DB. Returns True iff
        any revocation row with ``revoked_at_chain_seq <= chain_seq`` exists
        for the fingerprint.
        """
        if self.is_revoked_at(fingerprint, chain_seq):
            return True
        if self._engine is None:
            return False
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT 1 FROM governance_agent_key_revocations "
                    "WHERE key_fingerprint = :fp "
                    "AND revoked_at_chain_seq <= :seq LIMIT 1"
                ),
                {"fp": fingerprint, "seq": chain_seq},
            )
            row = res.first()
        return row is not None
