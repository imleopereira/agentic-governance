"""Pydantic models for Article 12 evidence reports.

Two models:
    ReportSection     - a single section mapping to an EU AI Act Article 12 requirement
    ComplianceReport  - the top-level evidence report containing all sections
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


SectionStatus = Literal["compliant", "partial", "non_compliant"]


class ReportSection(BaseModel):
    """A single section of an Article 12 evidence report.

    Each section maps to a specific EU AI Act Article 12 requirement.
    ``status`` reflects the state of the SDK-observed data for that requirement,
    not an assertion of compliance for the overall deployment.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=256)
    description: str = Field(max_length=4096)
    data: list[dict[str, Any]] = Field(default_factory=list)
    status: SectionStatus


class ComplianceReport(BaseModel):
    """Top-level Article 12 evidence report generated from audit trail data.

    Reports are immutable once generated. They can be serialized to JSON
    for archival or submission as evidence to regulators. The report does not
    assert compliance — it provides evidence for actions the SDK observed.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    report_id: UUID
    generated_at: datetime
    format: str = Field(min_length=1, max_length=64)
    agent_id: str | None = Field(default=None, max_length=256)
    session_ids: list[UUID] = Field(default_factory=list)
    date_range: tuple[datetime, datetime] | None = None
    sections: list[ReportSection] = Field(default_factory=list)
