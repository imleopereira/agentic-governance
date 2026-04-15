"""Agent public key registry (governance_agent_keys).

Two construction modes:

* ``AgentKeyRegistry()`` — in-memory, zero-config; used by unit tests and
  zero-config SDK instantiation per invariant #5.
* ``AgentKeyRegistry(engine=<AsyncEngine>)`` — SQLAlchemy async binding
  against the ``governance_agent_keys`` table created by migration
  ``f6a1ed25519aid``.

Both modes expose the legacy sync methods (``register``, ``get_by_fingerprint``,
``resolve``) used by the 32 existing unit tests. The engine-backed mode adds
async methods (``register_key``, ``lookup_key_by_fingerprint``,
``lookup_keys_for_agent``) that round-trip through Postgres.

Invariant: APPEND-ONLY. There is no update or delete API at any level. The
DB has ``REVOKE UPDATE, DELETE`` per the migration; the application mirrors
that by not exposing mutating paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class AgentKeyRecord:
    """Public-key registry row, mirrors ``governance_agent_keys``."""

    key_fingerprint: str
    agent_id: str
    public_key_pem: str
    activated_at_chain_seq: int


class AgentKeyRegistry:
    """Agent public-key registry — in-memory or SQLAlchemy-backed.

    In-memory mode is the default to satisfy invariant #5 (every public
    class instantiable with zero config in tests). Pass an ``AsyncEngine``
    to the constructor to bind to Postgres for production use.
    """

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine
        self._by_fingerprint: dict[str, AgentKeyRecord] = {}
        self._by_agent: dict[str, list[AgentKeyRecord]] = {}
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Legacy sync API — used by existing unit tests and the in-process
    # verifier on ephemeral/test paths. Always operates on the in-memory
    # mirror so existing callers keep working whether or not an engine is
    # attached.
    # ------------------------------------------------------------------

    def register(
        self,
        *,
        key_fingerprint: str,
        agent_id: str,
        public_key_pem: str,
        activated_at_chain_seq: int,
    ) -> AgentKeyRecord:
        """Append a new public key for ``agent_id`` (in-memory mirror).

        Globally unique on ``key_fingerprint`` — raises ``ValueError`` on
        a conflicting re-registration.
        """
        record = AgentKeyRecord(
            key_fingerprint=key_fingerprint,
            agent_id=agent_id,
            public_key_pem=public_key_pem,
            activated_at_chain_seq=activated_at_chain_seq,
        )
        with self._lock:
            if key_fingerprint in self._by_fingerprint:
                existing = self._by_fingerprint[key_fingerprint]
                if existing != record:
                    raise ValueError(
                        f"fingerprint {key_fingerprint} already registered "
                        f"with different contents"
                    )
                return existing
            self._by_fingerprint[key_fingerprint] = record
            self._by_agent.setdefault(agent_id, []).append(record)
        return record

    def get_by_fingerprint(self, fingerprint: str) -> AgentKeyRecord | None:
        return self._by_fingerprint.get(fingerprint)

    def resolve(self, agent_id: str, chain_seq: int) -> AgentKeyRecord | None:
        """Return the key row whose activation window contains ``chain_seq``.

        Latest ``activated_at_chain_seq <= chain_seq`` wins. Revocation is
        checked separately by ``RevocationStore``.
        """
        candidates = [
            r
            for r in self._by_agent.get(agent_id, [])
            if r.activated_at_chain_seq <= chain_seq
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.activated_at_chain_seq)

    # ------------------------------------------------------------------
    # Async SQLAlchemy API — requires ``engine`` at construction time.
    # ------------------------------------------------------------------

    async def register_key(
        self,
        agent_id: str,
        public_key_pem: str,
        activated_at_chain_seq: int,
        key_fingerprint: str | None = None,
    ) -> AgentKeyRecord:
        """Persist a public key to ``governance_agent_keys``.

        Also updates the in-memory mirror so sync lookups remain correct.
        Requires an ``AsyncEngine`` passed at construction time.
        """
        if self._engine is None:
            raise RuntimeError(
                "register_key requires an AsyncEngine — instantiate with "
                "AgentKeyRegistry(engine=...)."
            )
        from sqlalchemy import text  # local import: keep test path lean

        if key_fingerprint is None:
            from .signer import compute_fingerprint

            key_fingerprint = compute_fingerprint(public_key_pem)

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO governance_agent_keys "
                    "(key_fingerprint, agent_id, public_key_pem, "
                    "activated_at_chain_seq) VALUES "
                    "(:fp, :aid, :pem, :seq) "
                    "ON CONFLICT (key_fingerprint) DO NOTHING"
                ),
                {
                    "fp": key_fingerprint,
                    "aid": agent_id,
                    "pem": public_key_pem,
                    "seq": activated_at_chain_seq,
                },
            )
        return self.register(
            key_fingerprint=key_fingerprint,
            agent_id=agent_id,
            public_key_pem=public_key_pem,
            activated_at_chain_seq=activated_at_chain_seq,
        )

    async def lookup_key_by_fingerprint(
        self, fingerprint: str
    ) -> str | None:
        """Return the ``public_key_pem`` for a fingerprint, or None.

        Checks the in-memory mirror first (hot path for verification) and
        falls back to the DB on miss. Caches the DB result in the mirror.
        """
        cached = self._by_fingerprint.get(fingerprint)
        if cached is not None:
            return cached.public_key_pem
        if self._engine is None:
            return None
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT agent_id, public_key_pem, activated_at_chain_seq "
                    "FROM governance_agent_keys WHERE key_fingerprint = :fp "
                    "LIMIT 1"
                ),
                {"fp": fingerprint},
            )
            row = res.first()
        if row is None:
            return None
        # Cache in the mirror so subsequent lookups are instant.
        self.register(
            key_fingerprint=fingerprint,
            agent_id=row[0],
            public_key_pem=row[1],
            activated_at_chain_seq=row[2],
        )
        return row[1]

    async def lookup_keys_for_agent(
        self, agent_id: str
    ) -> list[AgentKeyRecord]:
        """Return all registered keys for ``agent_id`` in activation order."""
        if self._engine is None:
            return list(self._by_agent.get(agent_id, []))
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT key_fingerprint, public_key_pem, "
                    "activated_at_chain_seq "
                    "FROM governance_agent_keys WHERE agent_id = :aid "
                    "ORDER BY activated_at_chain_seq"
                ),
                {"aid": agent_id},
            )
            rows = list(res)
        out: list[AgentKeyRecord] = []
        for r in rows:
            rec = AgentKeyRecord(
                key_fingerprint=r[0],
                agent_id=agent_id,
                public_key_pem=r[1],
                activated_at_chain_seq=r[2],
            )
            out.append(rec)
        return out
