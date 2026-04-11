"""Tests for the compliance report generator."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from uuid import uuid4

import pytest

from codeatelier_governance.audit import AuditEvent, AuditModule, InMemoryAuditStore
from codeatelier_governance.compliance.models import ReportSection
from codeatelier_governance.compliance.report import ReportGenerator


# ---------------------------------------------------------------------------
# 1. Generate Article 12 report with audit events
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_article12_report_with_events(
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
    generator: ReportGenerator,
) -> None:
    """Article 12 report should generate successfully with audit events."""
    sid = uuid4()
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="billing-agent",
        kind="agent.start",
        input_hash="a" * 64,
        metadata={"tool": "read_invoice"},
    ))
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="billing-agent",
        kind="tool.call",
        input_hash="b" * 64,
        metadata={"tool": "read_invoice"},
    ))
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="billing-agent",
        kind="agent.end",
    ))
    # Flush to ensure events are written
    await asyncio.sleep(0.1)

    report = await generator.generate_article12(session_ids=[sid])
    assert report.format == "article12"
    assert len(report.sections) == 7
    assert report.report_id is not None
    assert report.generated_at is not None


# ---------------------------------------------------------------------------
# 2. Report includes all 7 required sections
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_article12_has_seven_sections(
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
    generator: ReportGenerator,
) -> None:
    """Article 12 report must contain exactly 7 sections mapping to the regulation."""
    sid = uuid4()
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="test-agent",
        kind="agent.start",
        input_hash="c" * 64,
    ))
    await asyncio.sleep(0.1)

    report = await generator.generate_article12(session_ids=[sid])

    expected_titles = [
        "Registration of Events",
        "Duration of Use",
        "Reference Database",
        "Input Data",
        "Functioning of the System",
        "Human Oversight Measures",
        "Post-Market Monitoring",
    ]
    actual_titles = [s.title for s in report.sections]
    assert actual_titles == expected_titles


# ---------------------------------------------------------------------------
# 3. Empty audit trail produces non_compliant sections
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_trail_non_compliant(
    generator: ReportGenerator,
) -> None:
    """An empty audit trail should result in non_compliant status across sections."""
    report = await generator.generate_article12()

    for section in report.sections:
        assert section.status == "non_compliant", (
            f"Section '{section.title}' should be non_compliant with no data, "
            f"got '{section.status}'"
        )


# ---------------------------------------------------------------------------
# 4. Date range filtering works
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_date_range_filtering(
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
    generator: ReportGenerator,
) -> None:
    """Events outside the date range should be excluded."""
    from datetime import datetime, timedelta, timezone

    sid = uuid4()
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="test-agent",
        kind="agent.start",
        input_hash="d" * 64,
    ))
    await asyncio.sleep(0.1)

    # Use a future date range that excludes the event
    future = datetime.now(timezone.utc) + timedelta(days=10)
    far_future = future + timedelta(days=10)

    report = await generator.generate_article12(
        date_from=future,
        date_to=far_future,
    )

    # Should get 0 events
    event_section = report.sections[0]  # Registration of Events
    event_count = next(
        d["value"] for d in event_section.data if d["metric"] == "total_events"
    )
    assert event_count == 0


# ---------------------------------------------------------------------------
# 5. Agent ID filtering works
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agent_id_filtering(
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
    generator: ReportGenerator,
) -> None:
    """Only events for the specified agent should appear in the report."""
    sid = uuid4()
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="billing-agent",
        kind="tool.call",
        input_hash="e" * 64,
        metadata={"tool": "read_invoice"},
    ))
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="support-agent",
        kind="tool.call",
        input_hash="f" * 64,
        metadata={"tool": "search_tickets"},
    ))
    await asyncio.sleep(0.1)

    report = await generator.generate_article12(agent_id="billing-agent")

    # Reference database section should only show billing-agent
    ref_section = report.sections[2]  # Reference Database
    agents = next(
        d["value"] for d in ref_section.data if d["metric"] == "agent_ids"
    )
    assert agents == ["billing-agent"]


# ---------------------------------------------------------------------------
# 6. Summary report includes violation counts
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_summary_includes_violations(
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
    generator: ReportGenerator,
) -> None:
    """Summary report should count violations correctly."""
    sid = uuid4()
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="risky-agent",
        kind="scope.violation",
        metadata={"tool": "forbidden_tool", "action": "denied"},
    ))
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="risky-agent",
        kind="budget.exceeded",
        metadata={"cap": "per_session_usd", "used": 5.0, "limit": 1.0},
    ))
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="risky-agent",
        kind="agent.start",
    ))
    await asyncio.sleep(0.1)

    report = await generator.generate_summary(agent_id="risky-agent")

    assert report.format == "summary"
    violation_section = report.sections[1]  # Violation Summary
    total_violations = next(
        d["value"] for d in violation_section.data
        if d["metric"] == "total_violations"
    )
    assert total_violations == 2
    assert violation_section.status == "partial"


# ---------------------------------------------------------------------------
# 7. Report model serializes to JSON correctly
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_report_serializes_to_json(
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
    generator: ReportGenerator,
) -> None:
    """ComplianceReport must round-trip through JSON serialization."""
    sid = uuid4()
    await audit.log(AuditEvent(
        session_id=sid,
        agent_id="test-agent",
        kind="agent.start",
        model="gpt-4",
        input_hash="g" * 64,
    ))
    await asyncio.sleep(0.1)

    report = await generator.generate_article12(session_ids=[sid])

    # Serialize
    json_str = report.model_dump_json(indent=2)
    parsed = json.loads(json_str)

    # Verify structure
    assert "report_id" in parsed
    assert "generated_at" in parsed
    assert "sections" in parsed
    assert len(parsed["sections"]) == 7
    assert parsed["format"] == "article12"

    # Verify each section has required fields
    for section in parsed["sections"]:
        assert "title" in section
        assert "description" in section
        assert "data" in section
        assert "status" in section
        assert section["status"] in ("compliant", "partial", "non_compliant")


# ---------------------------------------------------------------------------
# 8. CLI integration test
# ---------------------------------------------------------------------------
def test_cli_report_help() -> None:
    """The governance report subcommand should show help without errors."""
    result = subprocess.run(
        [sys.executable, "-m", "codeatelier_governance.cli", "report", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "compliance" in result.stdout.lower() or "report" in result.stdout.lower()


# ---------------------------------------------------------------------------
# 9. ReportSection model validation
# ---------------------------------------------------------------------------
def test_report_section_status_validation() -> None:
    """ReportSection status must be one of compliant/partial/non_compliant."""
    section = ReportSection(
        title="Test",
        description="A test section.",
        data=[{"metric": "x", "value": 1}],
        status="compliant",
    )
    assert section.status == "compliant"

    with pytest.raises(Exception):
        ReportSection(
            title="Test",
            description="A test section.",
            data=[],
            status="invalid_status",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 10. Article 12 sections show compliant with full data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_article12_compliant_with_rich_data(
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
    generator: ReportGenerator,
) -> None:
    """A well-instrumented agent should produce mostly compliant sections."""
    sid = uuid4()
    events = [
        AuditEvent(
            session_id=sid, agent_id="good-agent", kind="agent.start",
            model="gpt-4", input_hash="h" * 64,
        ),
        AuditEvent(
            session_id=sid, agent_id="good-agent", kind="tool.call",
            model="gpt-4", input_hash="i" * 64,
            metadata={"tool": "read_db"},
        ),
        AuditEvent(
            session_id=sid, agent_id="good-agent", kind="tool.result",
            model="gpt-4", input_hash="j" * 64,
        ),
        AuditEvent(
            session_id=sid, agent_id="good-agent", kind="approval.requested",
            input_hash="k" * 64,
        ),
        AuditEvent(
            session_id=sid, agent_id="good-agent", kind="approval.granted",
            input_hash="l" * 64,
        ),
        AuditEvent(
            session_id=sid, agent_id="good-agent", kind="agent.end",
        ),
    ]
    for event in events:
        await audit.log(event)
    await asyncio.sleep(0.1)

    report = await generator.generate_article12(session_ids=[sid])

    # Event registration and duration should be compliant
    assert report.sections[0].status == "partial"  # < 10 events = partial
    assert report.sections[1].status == "compliant"  # has sessions

    # Human oversight should be compliant (1 request, 1 grant)
    assert report.sections[5].status == "compliant"

    # Post-market monitoring: no violations, chain verified
    assert report.sections[6].status == "compliant"
