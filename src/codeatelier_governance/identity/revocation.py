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
    """Mirrors ``governance_agent_key_revocations``. Append-only.

    ``chain_event_id`` is the ``event_id`` of the ``audit.agent_key_revocation``
    chain row emitted at revocation time. Pre-v0.6.1 rows and in-memory
    revocations written without a chain linkage carry ``None``. Keeping
    this column makes the revocation table cross-reference the main
    chain, so tampering with the side table alone cannot hide a
    revocation — the chain row survives under HMAC + Ed25519.
    """

    key_fingerprint: str
    revoked_at_chain_seq: int
    reason: str
    operator_id: str
    created_at: datetime
    chain_event_id: Any = None  # UUID | None, kept ``Any`` to avoid uuid import cost


class RevocationStore:
    """Revocation log — in-memory or SQLAlchemy-backed.

    Two construction modes: zero-config in-memory (default; tests and
    ephemeral SDK mode) or Postgres-backed via an ``AsyncEngine``.

    **No public mutation API beyond** ``revoke``. Any code attempting to
    ``update`` or ``delete`` a revocation row is a policy violation and is
    rejected at the API level (the methods simply do not exist).
    """

    def __init__(self, engine: Any = None, *, strict_chain: bool = True) -> None:
        """Construct a revocation store.

        Parameters
        ----------
        engine:
            Optional SQLAlchemy ``AsyncEngine``. ``None`` means in-memory.
        strict_chain:
            v0.6.2 tamper-evidence hardening (Bug #9). When ``True`` (the
            default), ``revoke_with_chain_event`` RAISES if the audit chain
            write fails — no revocation row is appended without a
            ``chain_event_id``. This closes the silent-failure surface
            where an operator who can momentarily suppress audit writes
            (kill signal on audit worker, transient DB hiccup) could land
            a revocation that is NOT cross-bound to the chain — precisely
            the tamper surface ``revoke_with_chain_event`` was designed to
            close.

            Setting ``strict_chain=False`` preserves the v0.6.1 degraded
            behavior (mirror row written with ``chain_event_id=None``).
            This is intended for disaster-recovery scenarios where the
            operator has explicitly accepted the broken linkage. A
            distinct structured-log event
            (``identity.revocation_without_chain_event``) is emitted
            whenever a revocation lands in this mode so the SOC / audit
            reviewer can reconcile later.
        """
        self._engine = engine
        self._strict_chain = strict_chain
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
        chain_event_id: Any = None,
    ) -> RevocationRecord:
        """Append a revocation row (in-memory mirror).

        ``chain_event_id`` may be populated by ``revoke_with_chain_event``
        after emitting the tamper-evident chain row. The in-memory mirror
        does not require it.
        """
        record = RevocationRecord(
            key_fingerprint=key_fingerprint,
            revoked_at_chain_seq=revoked_at_chain_seq,
            reason=reason,
            operator_id=operator_id,
            created_at=datetime.now(timezone.utc),
            chain_event_id=chain_event_id,
        )
        with self._lock:
            self._records.append(record)
            self._by_fingerprint.setdefault(key_fingerprint, []).append(record)
        return record

    async def revoke_with_chain_event(
        self,
        *,
        audit_module: Any,
        key_fingerprint: str,
        reason: str,
        operator_id: str,
        revoker_agent_id: str = "governance.operator",
        strict_chain: bool | None = None,
    ) -> RevocationRecord:
        """Revoke a key AND emit an ``audit.agent_key_revocation`` chain row.

        v0.6.1 tamper-evidence upgrade: the revocation table by itself is
        only as trustworthy as its surrounding DB grants. Any operator
        with UPDATE/DELETE on ``governance_agent_key_revocations`` could
        retroactively rescind a revocation without the main audit chain
        noticing. By emitting a chain event we bind the revocation into
        the HMAC + Ed25519 chain — tampering with the side table is now
        detectable against the chain.

        Sequence of writes:
          1. Log ``audit.agent_key_revocation`` event to the audit chain
             (HMAC-linked, Ed25519-signed under the CURRENT active key).
          2. Use the resulting record's ``event_id`` and ``created_at``
             to populate the ``chain_event_id`` / ``revoked_at_chain_seq``
             columns on the revocation row.

        The revoked_at_chain_seq is looked up from the store after the
        event lands; if the store does not expose ``get_current_chain_seq``
        (e.g. in-memory mode), we use ``0`` which is safe because in-memory
        stores are not the enforcement substrate for production.

        v0.6.2 Bug #9 hardening: when ``strict_chain`` is True (the store
        default, overridable per-call), a failure to emit the chain row
        RAISES and NO revocation row is written. This closes the silent-
        failure surface where an operator who could momentarily suppress
        audit writes (kill signal on audit worker, transient DB hiccup)
        could land a revocation NOT cross-bound to the chain.

        Lax mode (``strict_chain=False``, either at store construction or
        per-call) preserves the v0.6.1 graceful-degradation behavior:
        on any failure in the chain write we still append the revocation
        to the in-memory mirror so the runtime enforcement path
        (``is_revoked_at``) works; a distinct WARN
        (``identity.revocation_without_chain_event``) is logged so
        operators can reconcile.
        """
        import structlog

        from ..audit.models import AuditEvent

        _log = structlog.get_logger(__name__)

        effective_strict = (
            self._strict_chain if strict_chain is None else strict_chain
        )

        event = AuditEvent(
            agent_id=revoker_agent_id,
            kind="audit.agent_key_revocation",
            metadata={
                "revoked_key_fingerprint": key_fingerprint,
                "reason": reason,
                "operator_id": operator_id,
            },
        )
        chain_event_id: Any = None
        revoked_seq = 0
        try:
            record = await audit_module.log(event)
            chain_event_id = record.event_id
        except Exception as exc:  # noqa: BLE001 — handled per strict_chain
            if effective_strict:
                # Strict mode (default): no row is appended without a
                # chain_event_id. Re-raise so the caller sees the
                # degraded audit substrate instead of a silently
                # unlinked revocation. See cybersecurity.md
                # "rotation-unaware verifiers / admin-bypass paths
                # missing distinct audit markers".
                _log.error(
                    "identity.revocation_chain_event_failed",
                    error_type=type(exc).__name__,
                    key_fingerprint_prefix=key_fingerprint[:16],
                    strict_chain=True,
                    detail=(
                        "Failed to emit audit.agent_key_revocation chain "
                        "row; refusing to append an unlinked revocation "
                        "(strict_chain=True). No row was written."
                    ),
                )
                raise
            # Lax mode: degraded-mode fallback. Distinct event name so a
            # SIEM rule can alert on any revocation that lands without a
            # chain linkage.
            _log.warning(
                "identity.revocation_without_chain_event",
                error_type=type(exc).__name__,
                key_fingerprint_prefix=key_fingerprint[:16],
                strict_chain=False,
                detail=(
                    "Failed to emit audit.agent_key_revocation chain row; "
                    "the revocation is still recorded in the side table "
                    "because strict_chain=False. Operators MUST manually "
                    "reconcile the chain — this row has chain_event_id=None."
                ),
            )

        # Resolve the chain_seq the revocation fires at. The chain event we
        # just emitted IS the first row the revocation governs — any row at
        # or after that seq signed by the revoked key must verify as
        # REVOKED_KEY. Looking it up from the store keeps the semantic
        # consistent with the sync in-memory revoke() path.
        #
        # TODO(v0.6.2): concurrency race — between ``audit_module.log``
        # and ``get_current_chain_seq``, other writers can land rows
        # signed by the revoked key. Those rows have chain_seq BETWEEN
        # our event's seq and the head returned here, so they are
        # incorrectly NOT rejected by the revocation window. The fix
        # requires surfacing ``chain_seq`` on the returned
        # ``AuditEventRecord`` (store-level change), so the revocation
        # row uses its OWN event's chain_seq as the boundary. Under a
        # single-writer deployment the current behavior is correct.
        store = getattr(audit_module, "_store", None)
        getter = getattr(store, "get_current_chain_seq", None)
        if getter is not None:
            try:
                revoked_seq = int(await getter())
            except Exception as exc:  # noqa: BLE001 — degrade gracefully
                _log.warning(
                    "identity.revocation_seq_lookup_failed",
                    error_type=type(exc).__name__,
                )

        # Persist via async path if available (writes to Postgres), else
        # fall back to the in-memory mirror.
        if self._engine is not None:
            try:
                return await self.revoke_async(
                    fingerprint=key_fingerprint,
                    revoked_at_chain_seq=revoked_seq,
                    reason=reason,
                    operator_id=operator_id,
                    chain_event_id=chain_event_id,
                )
            except Exception as exc:  # noqa: BLE001 — fall back to mirror
                _log.warning(
                    "identity.revocation_sql_failed",
                    error_type=type(exc).__name__,
                )
        return self.revoke(
            key_fingerprint=key_fingerprint,
            revoked_at_chain_seq=revoked_seq,
            reason=reason,
            operator_id=operator_id,
            chain_event_id=chain_event_id,
        )

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
        chain_event_id: Any = None,
    ) -> RevocationRecord:
        """Append a revocation row to Postgres AND the in-memory mirror.

        ``chain_event_id`` is the event_id of the
        ``audit.agent_key_revocation`` row emitted by
        ``revoke_with_chain_event``. Passing ``None`` is allowed so the
        existing SQL test path keeps working against schemas that
        pre-date the column — the INSERT uses COALESCE-style handling
        by simply omitting the column when the argument is None.
        """
        if self._engine is None:
            raise RuntimeError(
                "revoke_async requires an AsyncEngine — instantiate with "
                "RevocationStore(engine=...)."
            )
        from sqlalchemy import text

        # Best-effort: try to write chain_event_id if the column exists.
        # Schemas predating the v0.6.1 follow-up migration lack the column,
        # in which case the write falls back to the legacy 4-column INSERT.
        # DA v0.6.1: only fall through on a column-missing error — any
        # OTHER failure (connection drop, deadlock) must propagate so we
        # don't silently drop the chain_event_id linkage on transient
        # errors and corrupt the tamper-evidence story.
        if chain_event_id is not None:
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO governance_agent_key_revocations "
                            "(key_fingerprint, revoked_at_chain_seq, reason, "
                            "operator_id, chain_event_id) VALUES "
                            "(:fp, :seq, :reason, :op, :evt) "
                            "ON CONFLICT (key_fingerprint, revoked_at_chain_seq) "
                            "DO NOTHING"
                        ),
                        {
                            "fp": fingerprint,
                            "seq": revoked_at_chain_seq,
                            "reason": reason,
                            "op": operator_id,
                            "evt": str(chain_event_id),
                        },
                    )
                return self.revoke(
                    key_fingerprint=fingerprint,
                    revoked_at_chain_seq=revoked_at_chain_seq,
                    reason=reason,
                    operator_id=operator_id,
                    chain_event_id=chain_event_id,
                )
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                is_missing_column = (
                    ("does not exist" in msg or "undefined column" in msg)
                    and "chain_event_id" in msg
                )
                if not is_missing_column:
                    raise
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
            chain_event_id=chain_event_id,
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
