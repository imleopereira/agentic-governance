"""Pydantic models for Article 12 evidence reports.

Two models:
    ReportSection     - a single section mapping to an EU AI Act Article 12 requirement
    ComplianceReport  - the top-level evidence report containing all sections

Design note (v0.5.3):
    ``ComplianceReport`` deliberately does NOT carry a ``compliant`` /
    ``is_compliant`` boolean field.  The SDK can only observe actions that
    were routed through the SDK wrapper.  If the host application makes LLM
    calls outside the SDK, those events are invisible to the audit trail and
    therefore to this report.  Any single boolean verdict would imply 100 %
    coverage, which is a false assurance.  Instead, the report carries
    ``coverage_caveat`` (always populated) and ``coverage_pct`` (always
    present, currently ``None`` until a wrapper registry is implemented in
    v0.6) so consumers understand the scope limitation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


SectionStatus = Literal["compliant", "partial", "non_compliant"]

ChainIntegrityStatus = Literal["verified", "unverified", "failed"]

#: Required text for the coverage caveat.  Stored as a module-level constant
#: so the report generator and tests can reference the same string without
#: hard-coding it in multiple places.
COVERAGE_CAVEAT = (
    "This report covers only actions routed through the SDK. "
    "Calls made outside the SDK wrapper are not logged and are "
    "not reflected in this report."
)


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

    There is deliberately no ``compliant`` / ``is_compliant`` field.  See
    module docstring for rationale.  Consumers should interpret individual
    ``ReportSection.status`` values and read ``coverage_caveat`` to
    understand the scope of coverage before drawing conclusions.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    report_id: UUID
    generated_at: datetime
    format: str = Field(min_length=1, max_length=64)
    agent_id: str | None = Field(default=None, max_length=256)
    session_ids: list[UUID] = Field(default_factory=list)
    date_range: tuple[datetime, datetime] | None = None
    sections: list[ReportSection] = Field(default_factory=list)

    # --- v0.5.3 fields -------------------------------------------------------

    #: Summary of event counts and time range for the queried scope.
    event_count: int = Field(default=0, ge=0)
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None

    #: Whether the HMAC chain was verified as part of report generation.
    #: ``"verified"`` — chain was checked and passed.
    #: ``"unverified"`` — chain check was skipped (default for performance).
    #: ``"failed"`` — chain check was attempted and found a broken link.
    chain_integrity_status: ChainIntegrityStatus = "unverified"

    #: Always populated.  Never empty.  Reminds consumers that this report
    #: only covers events that passed through the SDK wrapper.
    coverage_caveat: str = Field(min_length=1)

    #: Fraction of total LLM calls captured by the SDK, or ``None`` when
    #: a total-call counter is not available.  Will be ``None`` in v0.5.x;
    #: populated in v0.6 when a wrapper registry is implemented.  The field
    #: must always be present — its absence would imply 100 % coverage.
    #: Must be in the range [0.0, 1.0] when provided.
    coverage_pct: float | None = Field(default=None, ge=0.0, le=1.0)

    #: DA-blocker discriminator for ``coverage_pct=None``.  Distinguishes:
    #:
    #: - ``"ok"``: ``coverage_pct`` is populated and meaningful.
    #: - ``"no_scope_policies_registered"``: denominator is zero (no governed
    #:   agents have been declared); ``coverage_pct`` is mathematically
    #:   undefined.
    #: - ``"registry_disabled"``: the F9 wrapper registry is not available
    #:   (engine absent, feature opt-out, or transient DB error).
    #: - ``None``: legacy v0.5.x reports reconstructed from old data, where
    #:   the field did not exist.
    #:
    #: A v0.6+ report MUST always populate this field.  Without it, an
    #: auditor cannot disambiguate "no agents declared" from "coverage
    #: measurement opted out", and the compliance artifact is ambiguous.
    coverage_pct_reason: Literal[
        "no_scope_policies_registered",
        "registry_disabled",
        "ok",
    ] | None = None

    @field_validator("coverage_caveat")
    @classmethod
    def _coverage_caveat_not_empty(cls, v: str) -> str:
        """Reject an empty coverage_caveat at model construction time."""
        if not v.strip():
            raise ValueError(
                "coverage_caveat must not be empty. "
                "Use COVERAGE_CAVEAT from codeatelier_governance.compliance.models."
            )
        return v
