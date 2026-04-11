"""Pydantic models for compliance reports.

Two models:
    ReportSection     - a single section mapping to an EU AI Act requirement
    ComplianceReport  - the top-level report containing all sections
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


SectionStatus = Literal["compliant", "partial", "non_compliant"]


class ReportSection(BaseModel):
    """A single section of a compliance report.

    Each section maps to a specific EU AI Act Article 12 requirement.
    ``status`` indicates whether the requirement is met based on available data.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=256)
    description: str = Field(max_length=4096)
    data: list[dict[str, Any]] = Field(default_factory=list)
    status: SectionStatus


class ComplianceReport(BaseModel):
    """Top-level compliance report generated from audit trail data.

    Reports are immutable once generated. They can be serialized to JSON
    for archival or submission to regulators.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    report_id: UUID
    generated_at: datetime
    format: str = Field(min_length=1, max_length=64)
    agent_id: str | None = Field(default=None, max_length=256)
    session_ids: list[UUID] = Field(default_factory=list)
    date_range: tuple[datetime, datetime] | None = None
    sections: list[ReportSection] = Field(default_factory=list)
