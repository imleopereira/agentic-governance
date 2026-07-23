"""Article 12 evidence report generator.

Queries audit events, cost data, gate resolutions, and scope policies
from Postgres (or in-memory stores for testing) to produce structured
evidence reports for actions the SDK observed.

All SQL is parameterized. No string interpolation in queries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog

from codeatelier_governance.audit.errors import ChainIntegrityError
from codeatelier_governance.audit.models import AuditEventRecord
from codeatelier_governance.audit.store import AuditStore, InMemoryAuditStore
from codeatelier_governance.audit.chain import (
    KEY_ROTATION_KIND,
    ChainVerifyRow,
    verify_chain_with_rotation,
)
from codeatelier_governance.audit.keys import KeyVersion

from . import article12
from .models import COVERAGE_CAVEAT, ChainIntegrityStatus, ComplianceReport, ReportSection

logger = structlog.get_logger(__name__)


class ReportGenerator:
    """Generates EU AI Act Article 12 evidence reports from audit data.

    Reports cover actions the SDK observed. They do not assert compliance —
    Article 12 compliance for a deployment depends on routing all relevant
    AI actions through the SDK.

    Can operate against either a Postgres database URL (for CLI usage)
    or an in-memory AuditStore (for tests and programmatic use).
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        audit_store: AuditStore | None = None,
        audit_module: Any | None = None,
        coverage: Any | None = None,
    ) -> None:
        """Initialize the report generator.

        Provide either ``database_url`` for Postgres queries or
        ``audit_store`` for in-memory/test usage. If both are provided,
        ``audit_store`` takes precedence.

        ``audit_module`` is an optional :class:`AuditModule` instance used
        when ``verify_chain=True`` is passed to ``generate_article12()`` or
        ``generate_summary()``. Without it, chain verification is unavailable
        and those methods will log a warning.
        """
        self._database_url = database_url
        self._audit_store = audit_store
        self._audit_module = audit_module
        # F9 collaborator: compute coverage_pct + coverage_pct_reason.
        # When not provided, falls back to a disabled computer that always
        # returns ("registry_disabled"). Mirrors the optional audit_module
        # injection pattern above.
        if coverage is None:
            from .coverage_stub import _DisabledCoverageComputer

            coverage = _DisabledCoverageComputer()
        self._coverage = coverage

    async def _query_events_postgres(
        self,
        *,
        session_ids: list[UUID] | None = None,
        agent_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Query audit events from Postgres with filters."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        if not self._database_url:
            return []

        engine = create_async_engine(self._database_url)
        try:
            where_clauses: list[str] = []
            params: dict[str, Any] = {}

            if session_ids:
                where_clauses.append(
                    "session_id = ANY(:session_ids)"
                )
                params["session_ids"] = [str(s) for s in session_ids]
            if agent_id:
                where_clauses.append("agent_id = :agent_id")
                params["agent_id"] = agent_id
            if date_from:
                where_clauses.append("created_at >= :date_from")
                params["date_from"] = date_from
            if date_to:
                where_clauses.append("created_at <= :date_to")
                params["date_to"] = date_to

            where = " AND ".join(where_clauses) if where_clauses else "TRUE"
            query = (
                f"SELECT event_id, session_id, agent_id, parent_event_id, "
                f"kind, model, input_hash, output_hash, metadata_json, "
                f"prev_hash, hmac_value, created_at "
                f"FROM governance_audit_events "
                f"WHERE {where} "
                f"ORDER BY created_at"
            )

            async with engine.connect() as conn:
                result = await conn.execute(text(query), params)
                rows = [dict(r._mapping) for r in result]
            return rows
        finally:
            await engine.dispose()

    async def _query_events_memory(
        self,
        *,
        session_ids: list[UUID] | None = None,
        agent_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[AuditEventRecord]:
        """Query events from in-memory audit store with filters."""
        if self._audit_store is None:
            return []

        # InMemoryAuditStore: iterate all events
        store = self._audit_store
        all_events: list[AuditEventRecord] = []

        if session_ids:
            for sid in session_ids:
                events = await store.get_session_events(sid)
                all_events.extend(events)
        elif hasattr(store, "_events"):
            # InMemoryAuditStore exposes _events dict
            mem_store: InMemoryAuditStore = store  # type: ignore[assignment]
            all_events = list(mem_store._events.values())
        else:
            return []

        # Apply filters
        filtered: list[AuditEventRecord] = []
        for event in all_events:
            if agent_id and event.agent_id != agent_id:
                continue
            if date_from and event.created_at < date_from:
                continue
            if date_to and event.created_at > date_to:
                continue
            filtered.append(event)

        # Sort by created_at
        filtered.sort(key=lambda e: e.created_at)
        return filtered

    def _events_to_dicts(
        self, events: list[AuditEventRecord],
    ) -> list[dict[str, Any]]:
        """Convert AuditEventRecord list to dict list for uniform processing."""
        return [
            {
                "event_id": str(e.event_id),
                "session_id": str(e.session_id),
                "agent_id": e.agent_id,
                "kind": e.kind,
                "model": e.model,
                "input_hash": e.input_hash,
                "output_hash": e.output_hash,
                "metadata_json": e.metadata,
                "created_at": e.created_at,
            }
            for e in events
        ]

    async def _get_events(
        self,
        *,
        session_ids: list[UUID] | None = None,
        agent_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Get events from either Postgres or in-memory store."""
        if self._audit_store is not None:
            records = await self._query_events_memory(
                session_ids=session_ids,
                agent_id=agent_id,
                date_from=date_from,
                date_to=date_to,
            )
            return self._events_to_dicts(records)
        return await self._query_events_postgres(
            session_ids=session_ids,
            agent_id=agent_id,
            date_from=date_from,
            date_to=date_to,
        )

    def _extract_report_data(
        self, events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Extract all metrics needed for Article 12 sections from events."""
        total = len(events)
        kinds: set[str] = set()
        agent_ids: set[str] = set()
        tools: set[str] = set()
        models: set[str] = set()
        sessions: dict[str, dict[str, Any]] = {}
        events_with_input_hash = 0
        scope_violations = 0
        budget_violations = 0
        loop_violations = 0
        approval_requested = 0
        approval_granted = 0
        approval_denied = 0
        date_start: str | None = None
        date_end: str | None = None

        for event in events:
            kind = event.get("kind", "")
            kinds.add(kind)

            aid = event.get("agent_id", "")
            if aid:
                agent_ids.add(aid)

            model = event.get("model")
            if model:
                models.add(model)

            if event.get("input_hash"):
                events_with_input_hash += 1

            created: Any = event.get("created_at")
            created_str: str = (
                created.isoformat() if hasattr(created, "isoformat") else str(created)
            )
            if date_start is None or created_str < date_start:
                date_start = created_str
            if date_end is None or created_str > date_end:
                date_end = created_str

            # Track sessions
            sid = str(event.get("session_id", ""))
            if sid:
                if sid not in sessions:
                    sessions[sid] = {"session_id": sid, "first_event": created_str}
                sessions[sid]["last_event"] = created_str

            # Track tool calls
            if kind == "tool.call":
                meta = event.get("metadata_json") or {}
                tool_name = meta.get("tool") or meta.get("tool_name")
                if tool_name:
                    tools.add(str(tool_name))

            # Count violations and approvals
            if kind == "scope.violation":
                scope_violations += 1
            elif kind == "budget.exceeded":
                budget_violations += 1
            elif kind == "loop.violation":
                loop_violations += 1
            elif kind == "approval.requested":
                approval_requested += 1
            elif kind == "approval.granted":
                approval_granted += 1
            elif kind == "approval.denied":
                approval_denied += 1

        return {
            "total_events": total,
            "kinds": sorted(kinds),
            "agent_ids": sorted(agent_ids),
            "tools": sorted(tools),
            "models": sorted(models),
            "sessions": list(sessions.values()),
            "events_with_input_hash": events_with_input_hash,
            "scope_violations": scope_violations,
            "budget_violations": budget_violations,
            "loop_violations": loop_violations,
            "approval_requested": approval_requested,
            "approval_granted": approval_granted,
            "approval_denied": approval_denied,
            "date_start": date_start,
            "date_end": date_end,
        }

    # DA blocker fix (F4): verify_chain is O(n). Running it over an
    # Article-12 retention history (6 months of events) times out and
    # creates a DoS vector. We window-limit to the most recent
    # ``_CHAIN_VERIFY_WINDOW`` events by default.
    _CHAIN_VERIFY_WINDOW: int = 1000

    async def _has_rotation_markers(self) -> bool:
        """BLOCKER C1: True if at least one chain key rotation marker exists.

        Used to decide whether to invoke the legacy single-key verifier
        (which HMACs every row under the CURRENT key — false positives
        and negatives on rotated chains) or the rotation-aware verifier.
        """
        # In-memory path
        if self._audit_store is not None:
            store = self._audit_store
            if hasattr(store, "_events"):
                for evt in store._events.values():
                    if getattr(evt, "kind", None) == KEY_ROTATION_KIND:
                        return True
            return False
        # Postgres path
        if self._database_url:
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import create_async_engine

            engine = create_async_engine(self._database_url)
            try:
                async with engine.connect() as conn:
                    res = await conn.execute(
                        text(
                            "SELECT COUNT(*) FROM governance_audit_events "
                            "WHERE kind = :k"
                        ),
                        {"k": KEY_ROTATION_KIND},
                    )
                    row = res.first()
                    return bool(row and int(row[0]) > 0)
            finally:
                await engine.dispose()
        return False

    async def _build_uri_map(self) -> dict[str, str]:
        """Construct fingerprint -> URI map from GOVERNANCE_CHAIN_KEY_<prefix> env vars.

        BLOCKER C1: the rotation-aware verifier needs to resolve historical
        key fingerprints back to material. Operators set
        ``GOVERNANCE_CHAIN_KEY_<first16>=env://VAR`` (or ``file://path``).
        We collect every such env var and build a fingerprint -> uri map.
        Resolution itself happens in audit.keys.resolve_key.
        """
        import os

        uri_map: dict[str, str] = {}
        # First load any registered key fingerprints so we can match prefix.
        versions = await self._load_key_versions()
        for kv in versions:
            prefix = kv.fingerprint[:16]
            env_name = f"GOVERNANCE_CHAIN_KEY_{prefix}"
            uri = os.environ.get(env_name)
            if uri:
                uri_map[kv.fingerprint] = uri
        return uri_map

    async def _load_key_versions(self) -> list[KeyVersion]:
        """Load all rows from governance_audit_chain_keys.

        Empty list when running against an in-memory store or when the
        table does not exist (pre-rotation deployments).
        """
        if self._database_url is None:
            return []
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(self._database_url)
        try:
            async with engine.connect() as conn:
                try:
                    res = await conn.execute(
                        text(
                            "SELECT key_version, fingerprint, "
                            "activated_at_chain_seq, retired_at_chain_seq "
                            "FROM governance_audit_chain_keys "
                            "ORDER BY activated_at_chain_seq"
                        )
                    )
                except Exception:  # noqa: BLE001 — table may not exist
                    return []
                rows = list(res.mappings())
        finally:
            await engine.dispose()
        return [
            KeyVersion(
                key_version=int(r["key_version"]),
                fingerprint=str(r["fingerprint"]),
                activated_at_chain_seq=int(r["activated_at_chain_seq"]),
                retired_at_chain_seq=(
                    int(r["retired_at_chain_seq"])
                    if r["retired_at_chain_seq"] is not None
                    else None
                ),
            )
            for r in rows
        ]

    async def _load_chain_rows(
        self, *, from_seq: int | None, to_seq: int | None
    ) -> list[ChainVerifyRow]:
        """Load rows for the rotation-aware verifier (Postgres only).

        Returns the rows in chain_seq order, restricted to
        ``[from_seq, to_seq]`` when provided.
        """
        if self._database_url is None:
            return []
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(self._database_url)
        try:
            where_clauses: list[str] = []
            params: dict[str, Any] = {}
            if from_seq is not None:
                where_clauses.append("chain_seq >= :from_seq")
                params["from_seq"] = from_seq
            if to_seq is not None:
                where_clauses.append("chain_seq <= :to_seq")
                params["to_seq"] = to_seq
            where = " AND ".join(where_clauses) if where_clauses else "TRUE"
            async with engine.connect() as conn:
                res = await conn.execute(
                    text(
                        f"SELECT chain_seq, event_id, session_id, agent_id, "
                        f"parent_event_id, kind, model, input_hash, output_hash, "
                        f"metadata_json, prev_hash, hmac_value, hmac_next, "
                        f"created_at, signature, signing_key_fingerprint, "
                        f"signature_status "
                        f"FROM governance_audit_events WHERE {where} "
                        f"ORDER BY chain_seq"
                    ),
                    params,
                )
                db_rows = list(res.mappings())
        finally:
            await engine.dispose()

        rows: list[ChainVerifyRow] = []
        for r in db_rows:
            rec = AuditEventRecord(
                event_id=r["event_id"],
                session_id=r["session_id"],
                agent_id=r["agent_id"],
                parent_event_id=r["parent_event_id"],
                kind=r["kind"],
                model=r["model"],
                input_hash=r["input_hash"],
                output_hash=r["output_hash"],
                metadata=r["metadata_json"] or {},
                prev_hash=r["prev_hash"],
                hmac=r["hmac_value"],
                created_at=r["created_at"],
                signature=r.get("signature"),
                signing_key_fingerprint=r.get("signing_key_fingerprint"),
                signature_status=r.get("signature_status") or "unsigned",
            )
            rows.append(
                ChainVerifyRow(
                    chain_seq=int(r["chain_seq"]),
                    record=rec,
                    hmac_next=r.get("hmac_next"),
                )
            )
        return rows

    async def _run_chain_verification(
        self,
        *,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> tuple[ChainIntegrityStatus, int | None, int | None, bool, list[str]]:
        """Run HMAC chain verification via the audit module and return the status string.

        Returns a tuple ``(status, verified_from_seq, verified_to_seq)``.
        ``status`` is one of :class:`ChainIntegrityStatus` literals:
        ``"verified"`` — chain checked and passed.
        ``"failed"`` — chain checked and found a broken or missing link.
        ``"unverified"`` — chain check skipped or errored.

        Never raises — errors are logged and mapped to ``"unverified"``.
        Callers must ensure ``self._audit_module`` is not None before calling;
        both ``generate_article12`` and ``generate_summary`` enforce this via
        an early ``ValueError`` guard.

        Window behavior: when ``from_seq``/``to_seq`` are not provided the
        verification scope is capped at the most recent
        ``_CHAIN_VERIFY_WINDOW`` events to prevent O(n) timeouts on long
        retention histories (DA blocker fix).
        """
        assert self._audit_module is not None  # enforced by callers
        # Default the window to the last _CHAIN_VERIFY_WINDOW events.
        if from_seq is None and to_seq is None:
            try:
                head_candidate = await self._current_chain_head()
            except Exception:  # noqa: BLE001 — defensive
                head_candidate = None
            if head_candidate is not None and head_candidate >= 0:
                window = self._CHAIN_VERIFY_WINDOW
                # head_candidate is the 0-based index of the last event.
                # Inclusive window: (head - 999) .. head spans 1000 events.
                from_seq = max(0, head_candidate - (window - 1))
                to_seq = head_candidate

        # BLOCKER C1: detect rotation markers and dispatch to the
        # rotation-aware verifier when present. The legacy single-key
        # verifier HMACs every row under the CURRENT GOVERNANCE_AUDIT_SECRET
        # which would silently false-positive or false-negative on a
        # rotated chain. The rotation-aware path resolves each row's
        # active key, returns ``unverified`` (NOT ``verified``) when an
        # old key fingerprint cannot be resolved, and surfaces unresolved
        # fingerprints to the operator.
        try:
            rotated = await self._has_rotation_markers()
        except Exception:  # noqa: BLE001 — defensive
            rotated = False

        if rotated:
            try:
                key_versions = await self._load_key_versions()
                uri_map = await self._build_uri_map()
                rows = await self._load_chain_rows(
                    from_seq=from_seq, to_seq=to_seq
                )
                result = verify_chain_with_rotation(
                    rows, key_versions, uri_map
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "compliance.report.rotation_verify_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return ("unverified", from_seq, to_seq, True, [])
            # Map the rotation-aware result back to the
            # ChainIntegrityStatus literal. CRITICAL: when keys are
            # unresolvable the status MUST be "unverified", never
            # "verified" — an attacker who knew an old key was missing
            # could otherwise hide tampering by ensuring the verifier
            # cannot read it.
            status: ChainIntegrityStatus
            if result.status == "ok":
                status = "verified"
            elif result.status == "failed":
                status = "failed"
            else:
                status = "unverified"
            return (
                status,
                from_seq,
                to_seq,
                True,
                list(result.unresolved_fingerprints),
            )

        # Legacy single-key path (no rotation marker present). On Postgres,
        # verify each row's HMAC over the loaded window: verify_chain needs an
        # in-memory index and cannot enumerate a Postgres store without a
        # session_id. Per-row HMAC detects in-place tampering; full-session
        # deletion detection is the `governance verify` CLI's job (a global
        # window spans sessions and cannot anchor a per-session genesis).
        try:
            rows = await self._load_chain_rows(from_seq=from_seq, to_seq=to_seq)
            if rows:
                if self._audit_module.verify_events_hmac([r.record for r in rows]):
                    return ("verified", from_seq, to_seq, False, [])
                logger.warning(
                    "compliance.report.chain_integrity_failed",
                    detail="per-row HMAC mismatch in the verified window",
                )
                return ("failed", from_seq, to_seq, False, [])
            # No Postgres rows (in-memory store, or empty window): fall back to
            # the in-memory chain verifier, which does anchor a genesis.
            await self._audit_module.verify_chain(from_seq=from_seq, to_seq=to_seq)
            return ("verified", from_seq, to_seq, False, [])
        except ChainIntegrityError as exc:
            logger.warning(
                "compliance.report.chain_integrity_failed",
                error=str(exc),
            )
            return ("failed", from_seq, to_seq, False, [])
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "compliance.report.chain_verify_error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return ("unverified", from_seq, to_seq, False, [])

    async def run_chain_verification_windowed(
        self,
        *,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> tuple[ChainIntegrityStatus, int | None, int | None, bool, list[str]]:
        """Public wrapper around :meth:`_run_chain_verification`.

        Used by the F4 console endpoints to run a windowed HMAC chain
        verification on demand (``POST /api/compliance/verify-chain``)
        and to expose the verified window on Article 12 reports.

        BLOCKER C1: returns a 5-tuple now —
        ``(status, from_seq, to_seq, rotation_aware, unresolved_fingerprints)``.
        ``rotation_aware`` is True iff a rotation marker exists in the chain
        and the rotation-aware verifier was used. ``unresolved_fingerprints``
        is non-empty only on the rotation-aware path.
        """
        return await self._run_chain_verification(
            from_seq=from_seq, to_seq=to_seq
        )

    async def _current_chain_head(self) -> int | None:
        """Return the 0-based index of the last event available to verify.

        For the in-memory store this is ``len(events) - 1`` across all
        sessions. For Postgres, this is ``COUNT(*) - 1``.  Returns ``None``
        when no events are available.
        """
        # In-memory path
        if self._audit_store is not None:
            store = self._audit_store
            if hasattr(store, "_events"):
                total = len(store._events)
                return (total - 1) if total > 0 else None
            return None
        # Postgres path
        if self._database_url:
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import create_async_engine

            engine = create_async_engine(self._database_url)
            try:
                async with engine.connect() as conn:
                    res = await conn.execute(
                        text("SELECT COUNT(*) FROM governance_audit_events")
                    )
                    row = res.first()
                    if row is None:
                        return None
                    total = int(row[0])
                    return (total - 1) if total > 0 else None
            finally:
                await engine.dispose()
        return None

    def _build_article12_sections(
        self, data: dict[str, Any],
    ) -> list[ReportSection]:
        """Build all 7 Article 12 sections from extracted data."""
        return [
            article12.section_event_registration(
                total_events=data["total_events"],
                date_range_start=data["date_start"],
                date_range_end=data["date_end"],
                event_kinds=data["kinds"],
            ),
            article12.section_duration_of_use(
                sessions=data["sessions"],
            ),
            article12.section_reference_database(
                agent_ids=data["agent_ids"],
                tools=data["tools"],
                models=data["models"],
            ),
            article12.section_input_data(
                events_with_input_hash=data["events_with_input_hash"],
                total_events=data["total_events"],
            ),
            article12.section_functioning(
                scope_policies_count=len(data["tools"]),
                budget_policies_count=data["budget_violations"]
                + (1 if data["total_events"] > 0 else 0),
                hitl_gates_count=data["approval_requested"],
            ),
            article12.section_human_oversight(
                approval_requested=data["approval_requested"],
                approval_granted=data["approval_granted"],
                approval_denied=data["approval_denied"],
            ),
            article12.section_post_market_monitoring(
                scope_violations=data["scope_violations"],
                budget_violations=data["budget_violations"],
                loop_violations=data["loop_violations"],
                # chain_integrity_verified is always False here: the report
                # generator does not perform HMAC verification. Callers who need
                # a verified status should call sdk.audit.verify_chain() and pass
                # the result via generate_article12(verify_chain=True).
                chain_integrity_verified=False,
            ),
        ]

    async def generate_article12(
        self,
        session_ids: list[UUID] | None = None,
        agent_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        *,
        verify_chain: bool = False,
    ) -> ComplianceReport:
        """Generate an EU AI Act Article 12 evidence report.

        Queries audit events with the given filters and maps them to the
        seven Article 12 automatic logging requirements. The report provides
        evidence for actions the SDK observed; it does not assert compliance
        for the overall deployment.

        Args:
            session_ids: Optional list of session UUIDs to filter events.
            agent_id: Optional agent identifier to filter events.
            date_from: Optional start of the date range filter.
            date_to: Optional end of the date range filter.
            verify_chain: When ``True``, runs HMAC chain verification via the
                ``audit_module`` provided at construction time and sets
                ``chain_integrity_status`` to ``"verified"`` or ``"failed"``.
                Detects tampering, head/interior deletion, and reordering; it
                does NOT detect tail truncation (see ``audit.verify_chain``).
                Requires ``audit_module`` to be set. Default ``False``.

        Raises:
            ValueError: If ``verify_chain=True`` is requested but no
                ``audit_module`` was provided at construction time.
        """
        if verify_chain and self._audit_module is None:
            raise ValueError(
                "verify_chain=True requires an audit_module to be passed to "
                "ReportGenerator at construction time. "
                "Pass audit_module=sdk.audit when constructing ReportGenerator."
            )
        events = await self._get_events(
            session_ids=session_ids,
            agent_id=agent_id,
            date_from=date_from,
            date_to=date_to,
        )

        data = self._extract_report_data(events)
        sections = self._build_article12_sections(data)
        coverage_pct, coverage_reason = await self._coverage.compute(
            agent_id=agent_id,
        )

        if verify_chain:
            (
                chain_integrity_status,
                _cv_from,
                _cv_to,
                _cv_rotation_aware,
                _cv_unresolved,
            ) = await self._run_chain_verification()
        else:
            chain_integrity_status = "unverified"
            _cv_from = None
            _cv_to = None
            _cv_rotation_aware = False
            _cv_unresolved = []

        now = datetime.now(timezone.utc)
        # DA Wave 4 blocker fix (F4): persist the verified window on the
        # report so the console handler does not have to re-run verify_chain
        # a second time just to recover from_seq/to_seq.
        session_id_list: list[UUID] = []
        if session_ids:
            session_id_list = list(session_ids)
        else:
            for s in data["sessions"]:
                try:
                    session_id_list.append(UUID(s["session_id"]))
                except (ValueError, KeyError):
                    pass

        date_range: tuple[datetime, datetime] | None = None
        if date_from and date_to:
            date_range = (date_from, date_to)

        # Parse time_range_start / time_range_end from extracted data
        time_range_start: datetime | None = None
        time_range_end: datetime | None = None
        if data["date_start"]:
            try:
                time_range_start = datetime.fromisoformat(data["date_start"])
            except (ValueError, TypeError):
                pass
        if data["date_end"]:
            try:
                time_range_end = datetime.fromisoformat(data["date_end"])
            except (ValueError, TypeError):
                pass

        return ComplianceReport(
            report_id=uuid4(),
            generated_at=now,
            format="article12",
            agent_id=agent_id,
            session_ids=session_id_list,
            date_range=date_range,
            sections=sections,
            event_count=data["total_events"],
            time_range_start=time_range_start,
            time_range_end=time_range_end,
            chain_integrity_status=chain_integrity_status,
            chain_verified_from_seq=_cv_from,
            chain_verified_to_seq=_cv_to,
            rotation_aware=_cv_rotation_aware,
            unresolved_fingerprints=_cv_unresolved,
            coverage_caveat=COVERAGE_CAVEAT,
            coverage_pct=coverage_pct,
            coverage_pct_reason=coverage_reason,
        )

    async def generate_summary(
        self,
        agent_id: str,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        *,
        verify_chain: bool = False,
    ) -> ComplianceReport:
        """Generate a summary evidence report for an agent.

        Provides a high-level overview including event counts, violation
        counts, and status per section based on SDK-observed data.

        Args:
            agent_id: The agent identifier whose events to summarize.
            date_from: Optional start of the date range filter.
            date_to: Optional end of the date range filter.
            verify_chain: When ``True``, runs HMAC chain verification via the
                ``audit_module`` provided at construction time and sets
                ``chain_integrity_status`` to ``"verified"`` or ``"failed"``.
                Detects tampering, head/interior deletion, and reordering; it
                does NOT detect tail truncation (see ``audit.verify_chain``).
                Requires ``audit_module`` to be set. Default ``False``.

        Raises:
            ValueError: If ``verify_chain=True`` is requested but no
                ``audit_module`` was provided at construction time.
        """
        if verify_chain and self._audit_module is None:
            raise ValueError(
                "verify_chain=True requires an audit_module to be passed to "
                "ReportGenerator at construction time. "
                "Pass audit_module=sdk.audit when constructing ReportGenerator."
            )
        events = await self._get_events(
            agent_id=agent_id,
            date_from=date_from,
            date_to=date_to,
        )

        data = self._extract_report_data(events)
        total = data["total_events"]
        coverage_pct, coverage_reason = await self._coverage.compute(
            agent_id=agent_id,
        )
        if verify_chain:
            (
                chain_integrity_status,
                _cv_from,
                _cv_to,
                _cv_rotation_aware,
                _cv_unresolved,
            ) = await self._run_chain_verification()
        else:
            chain_integrity_status = "unverified"
            _cv_from = None
            _cv_to = None
            _cv_rotation_aware = False
            _cv_unresolved = []
        total_violations = (
            data["scope_violations"]
            + data["budget_violations"]
            + data["loop_violations"]
        )

        sections = [
            ReportSection(
                title="Agent Overview",
                description=f"Summary for agent '{agent_id}'.",
                data=[
                    {"metric": "agent_id", "value": agent_id},
                    {"metric": "total_events", "value": total},
                    {"metric": "total_sessions", "value": len(data["sessions"])},
                    {"metric": "event_kinds", "value": data["kinds"]},
                    {"metric": "models_used", "value": data["models"]},
                    {"metric": "date_range_start", "value": data["date_start"]},
                    {"metric": "date_range_end", "value": data["date_end"]},
                ],
                status="compliant" if total > 0 else "non_compliant",
            ),
            ReportSection(
                title="Violation Summary",
                description="Policy violations observed in the audit trail.",
                data=[
                    {"metric": "scope_violations", "value": data["scope_violations"]},
                    {"metric": "budget_violations", "value": data["budget_violations"]},
                    {"metric": "loop_violations", "value": data["loop_violations"]},
                    {"metric": "total_violations", "value": total_violations},
                ],
                status=(
                    "compliant" if total_violations == 0 and total > 0
                    else "partial" if total > 0
                    else "non_compliant"
                ),
            ),
            ReportSection(
                title="Human Oversight",
                description="HITL gate activity for the agent.",
                data=[
                    {"metric": "approval_requested", "value": data["approval_requested"]},
                    {"metric": "approval_granted", "value": data["approval_granted"]},
                    {"metric": "approval_denied", "value": data["approval_denied"]},
                ],
                status=(
                    "compliant" if data["approval_requested"] > 0
                    else "non_compliant"
                ),
            ),
        ]

        now = datetime.now(timezone.utc)
        session_id_list: list[UUID] = []
        for s in data["sessions"]:
            try:
                session_id_list.append(UUID(s["session_id"]))
            except (ValueError, KeyError):
                pass

        date_range: tuple[datetime, datetime] | None = None
        if date_from and date_to:
            date_range = (date_from, date_to)

        # Parse time_range_start / time_range_end from extracted data
        summary_time_start: datetime | None = None
        summary_time_end: datetime | None = None
        if data["date_start"]:
            try:
                summary_time_start = datetime.fromisoformat(data["date_start"])
            except (ValueError, TypeError):
                pass
        if data["date_end"]:
            try:
                summary_time_end = datetime.fromisoformat(data["date_end"])
            except (ValueError, TypeError):
                pass

        return ComplianceReport(
            report_id=uuid4(),
            generated_at=now,
            format="summary",
            agent_id=agent_id,
            session_ids=session_id_list,
            date_range=date_range,
            sections=sections,
            event_count=data["total_events"],
            time_range_start=summary_time_start,
            time_range_end=summary_time_end,
            chain_integrity_status=chain_integrity_status,
            chain_verified_from_seq=_cv_from,
            chain_verified_to_seq=_cv_to,
            rotation_aware=_cv_rotation_aware,
            unresolved_fingerprints=_cv_unresolved,
            coverage_caveat=COVERAGE_CAVEAT,
            coverage_pct=coverage_pct,
            coverage_pct_reason=coverage_reason,
        )
