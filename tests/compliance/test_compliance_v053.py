"""Tests for Item 6: compliance report changes in v0.5.3.

Requirements:
    1. article12_report() return value has NO field named `compliant`,
       `is_compliant`, or any boolean compliance verdict.
    2. Return value has `coverage_caveat` field that is non-empty.
    3. Return value has `coverage_pct` field (None in v0.5.x).
    4. coverage_caveat cannot be empty string (validator test).
    5. chain_integrity_status is one of "verified", "unverified", "failed".
"""
from __future__ import annotations

import pytest
from uuid import uuid4

from codeatelier_governance.audit import AuditEvent, AuditModule, InMemoryAuditStore
from codeatelier_governance.compliance.models import (
    COVERAGE_CAVEAT,
    ComplianceReport,
)
from codeatelier_governance.compliance.report import ReportGenerator


# ---------------------------------------------------------------------------
# Test 1: No compliance verdict fields on the report
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_report_has_no_compliance_verdict(
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
    generator: ReportGenerator,
) -> None:
    """ComplianceReport must have no `compliant`, `is_compliant`, or similar field."""
    sid = uuid4()
    await audit.log(
        AuditEvent(session_id=sid, agent_id="agent-a", kind="agent.start")
    )
    await audit._writer.flush()

    report = await generator.generate_article12(session_ids=[sid])

    # Direct attribute checks
    assert not hasattr(report, "compliant"), "report must not have 'compliant' field"
    assert not hasattr(report, "is_compliant"), "report must not have 'is_compliant' field"

    # Also verify via the serialized dict — no boolean key resembling a verdict
    report_dict = report.model_dump()
    for key in report_dict:
        assert key not in ("compliant", "is_compliant"), (
            f"Serialized report must not include compliance verdict key '{key}'"
        )


# ---------------------------------------------------------------------------
# Test 2: coverage_caveat is present and non-empty
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_report_has_non_empty_coverage_caveat(
    generator: ReportGenerator,
) -> None:
    """coverage_caveat must be present and must not be empty."""
    report = await generator.generate_article12()

    assert hasattr(report, "coverage_caveat")
    assert isinstance(report.coverage_caveat, str)
    assert len(report.coverage_caveat.strip()) > 0, "coverage_caveat must not be empty"


# ---------------------------------------------------------------------------
# Test 3: coverage_pct field exists (value is None in v0.5.x)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_report_has_coverage_pct_field(
    generator: ReportGenerator,
) -> None:
    """coverage_pct must be present on the report; its value is None for v0.5.x."""
    report = await generator.generate_article12()

    assert hasattr(report, "coverage_pct"), "report must have 'coverage_pct' field"
    # In v0.5.x the wrapper registry doesn't exist yet; must always be None.
    assert report.coverage_pct is None, (
        "coverage_pct must be None in v0.5.x (wrapper registry not yet implemented)"
    )

    # Same for summary reports
    report_summary = await generator.generate_summary(agent_id="test-agent")
    assert hasattr(report_summary, "coverage_pct")
    assert report_summary.coverage_pct is None


# ---------------------------------------------------------------------------
# Test 4: coverage_caveat validator rejects empty strings
# ---------------------------------------------------------------------------
def test_coverage_caveat_cannot_be_empty() -> None:
    """ComplianceReport must reject an empty coverage_caveat at model construction."""
    import pytest

    with pytest.raises(Exception):
        ComplianceReport(
            report_id=uuid4(),
            generated_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            format="article12",
            coverage_caveat="",  # must be rejected
            event_count=0,
            chain_integrity_status="unverified",
        )

    with pytest.raises(Exception):
        ComplianceReport(
            report_id=uuid4(),
            generated_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            format="article12",
            coverage_caveat="   ",  # whitespace only — also rejected
            event_count=0,
            chain_integrity_status="unverified",
        )


# ---------------------------------------------------------------------------
# Test 5: chain_integrity_status is one of the allowed literals
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chain_integrity_status_valid_literal(
    generator: ReportGenerator,
) -> None:
    """chain_integrity_status must be 'verified', 'unverified', or 'failed'."""
    report = await generator.generate_article12()

    assert hasattr(report, "chain_integrity_status")
    assert report.chain_integrity_status in ("verified", "unverified", "failed"), (
        f"Expected one of verified/unverified/failed, "
        f"got {report.chain_integrity_status!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: The COVERAGE_CAVEAT constant is substantively correct
# ---------------------------------------------------------------------------
def test_coverage_caveat_constant_content() -> None:
    """The module-level COVERAGE_CAVEAT constant must mention 'outside the SDK'
    (or equivalent) to communicate the coverage limitation."""
    caveat = COVERAGE_CAVEAT
    assert isinstance(caveat, str)
    # The text must convey that calls outside the SDK are not logged.
    lowered = caveat.lower()
    assert "outside" in lowered or "not logged" in lowered or "not reflected" in lowered, (
        f"COVERAGE_CAVEAT must mention calls outside the SDK wrapper: {caveat!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: generate_summary also carries the new fields
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_summary_report_has_new_fields(
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
    generator: ReportGenerator,
) -> None:
    """generate_summary() must also carry coverage_caveat, coverage_pct,
    chain_integrity_status, and event_count."""
    sid = uuid4()
    await audit.log(
        AuditEvent(session_id=sid, agent_id="sum-agent", kind="agent.start")
    )
    await audit._writer.flush()

    report = await generator.generate_summary(agent_id="sum-agent")

    assert hasattr(report, "coverage_caveat")
    assert len(report.coverage_caveat.strip()) > 0
    assert hasattr(report, "coverage_pct")
    assert report.coverage_pct is None
    assert hasattr(report, "chain_integrity_status")
    assert report.chain_integrity_status in ("verified", "unverified", "failed")
    assert hasattr(report, "event_count")
    assert isinstance(report.event_count, int)
    assert not hasattr(report, "compliant")
    assert not hasattr(report, "is_compliant")
