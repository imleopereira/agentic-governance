"""Tests for the F9 ``coverage_pct_reason`` discriminator field.

Closes the DA blocker: a v0.6 compliance report must always populate
``coverage_pct_reason`` so consumers can disambiguate ``coverage_pct=None``
between "denominator is zero" and "registry disabled".
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from codeatelier_governance.compliance.models import (
    COVERAGE_CAVEAT,
    ComplianceReport,
)
from codeatelier_governance.compliance.report import ReportGenerator


def _base_kwargs() -> dict[str, Any]:
    return {
        "report_id": uuid4(),
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc,
        ),
        "format": "article12",
        "sections": [],
        "coverage_caveat": COVERAGE_CAVEAT,
    }


def test_coverage_pct_reason_field_exists_and_defaults_to_none() -> None:
    report = ComplianceReport(**_base_kwargs())
    assert hasattr(report, "coverage_pct_reason")
    assert report.coverage_pct_reason is None


def test_coverage_pct_reason_accepts_ok() -> None:
    report = ComplianceReport(
        coverage_pct=0.8,
        coverage_pct_reason="ok",
        **_base_kwargs(),
    )
    assert report.coverage_pct_reason == "ok"
    assert report.coverage_pct == 0.8


def test_coverage_pct_reason_accepts_no_scope_policies() -> None:
    report = ComplianceReport(
        coverage_pct=None,
        coverage_pct_reason="no_scope_policies_registered",
        **_base_kwargs(),
    )
    assert report.coverage_pct_reason == "no_scope_policies_registered"
    assert report.coverage_pct is None


def test_coverage_pct_reason_accepts_registry_disabled() -> None:
    report = ComplianceReport(
        coverage_pct=None,
        coverage_pct_reason="registry_disabled",
        **_base_kwargs(),
    )
    assert report.coverage_pct_reason == "registry_disabled"


def test_coverage_pct_reason_rejects_unknown_literal() -> None:
    with pytest.raises(ValidationError):
        ComplianceReport(
            coverage_pct_reason="not-a-real-reason",  # type: ignore[arg-type]
            **_base_kwargs(),
        )


@pytest.mark.asyncio
async def test_generator_default_uses_disabled_stub() -> None:
    """A generator constructed without a coverage collaborator must still
    return a valid report with a populated discriminator — not a bare None."""
    from codeatelier_governance.audit.store import InMemoryAuditStore

    gen = ReportGenerator(audit_store=InMemoryAuditStore())
    report = await gen.generate_article12()
    assert report.coverage_pct is None
    assert report.coverage_pct_reason == "registry_disabled"


@pytest.mark.asyncio
async def test_generator_summary_coverage_reason_populated() -> None:
    from codeatelier_governance.audit.store import InMemoryAuditStore

    gen = ReportGenerator(audit_store=InMemoryAuditStore())
    report = await gen.generate_summary(agent_id="a1")
    assert report.coverage_pct_reason == "registry_disabled"


@pytest.mark.asyncio
async def test_generator_accepts_injected_coverage_collaborator() -> None:
    from codeatelier_governance.audit.store import InMemoryAuditStore

    class FakeCoverage:
        async def compute(
            self, *, agent_id: str | None = None,
        ) -> tuple[float | None, str | None]:
            return (0.42, "ok")

    gen = ReportGenerator(
        audit_store=InMemoryAuditStore(),
        coverage=FakeCoverage(),
    )
    report = await gen.generate_article12()
    assert report.coverage_pct == 0.42
    assert report.coverage_pct_reason == "ok"
