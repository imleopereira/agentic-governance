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

from codeatelier_governance.audit.models import AuditEventRecord
from codeatelier_governance.audit.store import AuditStore, InMemoryAuditStore

from . import article12
from .models import COVERAGE_CAVEAT, ComplianceReport, ReportSection

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
    ) -> None:
        """Initialize the report generator.

        Provide either ``database_url`` for Postgres queries or
        ``audit_store`` for in-memory/test usage. If both are provided,
        ``audit_store`` takes precedence.
        """
        self._database_url = database_url
        self._audit_store = audit_store

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
                chain_integrity_verified=data["total_events"] > 0,
            ),
        ]

    async def generate_article12(
        self,
        session_ids: list[UUID] | None = None,
        agent_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> ComplianceReport:
        """Generate an EU AI Act Article 12 evidence report.

        Queries audit events with the given filters and maps them to the
        seven Article 12 automatic logging requirements. The report provides
        evidence for actions the SDK observed; it does not assert compliance
        for the overall deployment.
        """
        events = await self._get_events(
            session_ids=session_ids,
            agent_id=agent_id,
            date_from=date_from,
            date_to=date_to,
        )

        data = self._extract_report_data(events)
        sections = self._build_article12_sections(data)

        now = datetime.now(timezone.utc)
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
            chain_integrity_status="unverified",
            coverage_caveat=COVERAGE_CAVEAT,
            coverage_pct=None,
        )

    async def generate_summary(
        self,
        agent_id: str,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> ComplianceReport:
        """Generate a summary evidence report for an agent.

        Provides a high-level overview including event counts, violation
        counts, and status per section based on SDK-observed data.
        """
        events = await self._get_events(
            agent_id=agent_id,
            date_from=date_from,
            date_to=date_to,
        )

        data = self._extract_report_data(events)
        total = data["total_events"]
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
            chain_integrity_status="unverified",
            coverage_caveat=COVERAGE_CAVEAT,
            coverage_pct=None,
        )
