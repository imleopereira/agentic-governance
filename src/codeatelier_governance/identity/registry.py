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

        v0.6.1 stricter boundary: ``activated_at_chain_seq`` must be
        strictly greater than any existing key's activation seq for the
        same ``agent_id``. Two keys sharing an activation seq (or a new
        key activating before the latest one) creates a range overlap
        where ``AgentKeyRegistry.resolve(agent_id, seq)`` is ambiguous
        at the boundary. Reject at load/register time rather than let
        the ambiguity leak into runtime verification.

        Re-registering the SAME ``key_fingerprint`` with identical
        contents remains idempotent (no error) so retry loops are safe.
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
            # Monotonic activation_seq per agent — reject overlaps.
            existing_for_agent = self._by_agent.get(agent_id, [])
            for prior in existing_for_agent:
                if prior.activated_at_chain_seq >= activated_at_chain_seq:
                    raise ValueError(
                        f"agent_id={agent_id!r} activation_seq overlap: "
                        f"new key {key_fingerprint[:16]}... at seq="
                        f"{activated_at_chain_seq} is not strictly after "
                        f"existing key {prior.key_fingerprint[:16]}... at "
                        f"seq={prior.activated_at_chain_seq}. Activation "
                        f"sequences for a single agent must be strictly "
                        f"monotonic; rotate by appending with a HIGHER "
                        f"activated_at_chain_seq than the current head."
                    )
            self._by_fingerprint[key_fingerprint] = record
            self._by_agent.setdefault(agent_id, []).append(record)
        return record

    def _cache_existing(
        self,
        *,
        key_fingerprint: str,
        agent_id: str,
        public_key_pem: str,
        activated_at_chain_seq: int,
    ) -> AgentKeyRecord:
        """Hydrate an ALREADY-PERSISTED key into the in-memory mirror.

        v0.6.1: the monotonic-activation_seq check on ``register`` is a
        LOAD-time invariant — it rejects NEW registrations that would
        create an ambiguous resolve() window. It MUST NOT fire when we
        are merely caching a historical row that the DB already accepted
        (e.g. on-demand lookup of an older key during chain
        verification). If the check fires there, ``resolve(agent_id,
        old_seq)`` breaks for legitimate, correctly-ordered rotations.

        This helper is internal: callers outside the registry must use
        ``register`` (which enforces monotonic order) or
        ``register_key`` (which persists + mirrors).
        """
        record = AgentKeyRecord(
            key_fingerprint=key_fingerprint,
            agent_id=agent_id,
            public_key_pem=public_key_pem,
            activated_at_chain_seq=activated_at_chain_seq,
        )
        with self._lock:
            if key_fingerprint in self._by_fingerprint:
                return self._by_fingerprint[key_fingerprint]
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

        Security v0.6.1: the sync ``register`` method enforces strict
        monotonic ``activated_at_chain_seq`` per-agent (single-process,
        via the in-memory mirror). Across processes, the DB has a UNIQUE
        constraint on ``key_fingerprint`` but **not** on
        ``(agent_id, activated_at_chain_seq)``. Two processes racing to
        register DIFFERENT keys at the SAME seq for the same agent would
        both land in the DB and only the second would raise ``ValueError``
        from the local mirror check.

        Defense-in-depth: after the INSERT we re-query for any OTHER row
        at the same ``(agent_id, activated_at_chain_seq)`` and raise
        ``ValueError`` if one exists. This is not fully atomic (TOCTOU),
        but narrows the race window from "DB write" to "post-INSERT
        SELECT", and guarantees at least one process in a concurrent pair
        surfaces the overlap loudly rather than silently accepting an
        ambiguous ``resolve()`` window. A full fix requires a
        ``UNIQUE (agent_id, activated_at_chain_seq)`` migration — tracked
        as TODO(v0.6.2). In single-writer deployments the current guard
        is already sufficient.
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
            # Post-INSERT overlap check: any OTHER fingerprint sharing
            # (agent_id, activated_at_chain_seq) with us is a concurrent-
            # registration collision. Raise so the caller can retry with
            # a higher seq rather than leak an ambiguous resolve window.
            res = await conn.execute(
                text(
                    "SELECT key_fingerprint FROM governance_agent_keys "
                    "WHERE agent_id = :aid "
                    "AND activated_at_chain_seq = :seq "
                    "AND key_fingerprint <> :fp "
                    "LIMIT 1"
                ),
                {
                    "aid": agent_id,
                    "seq": activated_at_chain_seq,
                    "fp": key_fingerprint,
                },
            )
            collision = res.first()
        if collision is not None:
            raise ValueError(
                f"agent_id={agent_id!r} activation_seq collision at "
                f"seq={activated_at_chain_seq}: a different key "
                f"{str(collision[0])[:16]}... was persisted concurrently. "
                f"Retry with a strictly higher activated_at_chain_seq."
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
        # Cache in the mirror so subsequent lookups are instant. Use the
        # internal ``_cache_existing`` path — this is an already-persisted
        # historical row, not a new registration, so the monotonic
        # activation_seq check must not fire (it would raise on an older
        # key for an agent whose newer key is already mirrored, breaking
        # legitimate verify-historical-row paths).
        self._cache_existing(
            key_fingerprint=fingerprint,
            agent_id=row[0],
            public_key_pem=row[1],
            activated_at_chain_seq=row[2],
        )
        return str(row[1])

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
