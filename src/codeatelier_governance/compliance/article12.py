"""EU AI Act Article 12 compliance mapping.

Maps audit trail data to the seven automatic logging requirements
defined in Article 12 of the EU AI Act (enforcement: 2026-08-02).

Each section function receives raw query results and returns a
ReportSection with status assessment.
"""
from __future__ import annotations

from typing import Any

from .models import ReportSection, SectionStatus


def _status_from_count(count: int, *, partial_threshold: int = 1) -> SectionStatus:
    """Derive status from a count: 0 = non_compliant, < threshold = partial, else compliant."""
    if count == 0:
        return "non_compliant"
    if count < partial_threshold:
        return "partial"
    return "compliant"


def section_event_registration(
    *,
    total_events: int,
    date_range_start: str | None,
    date_range_end: str | None,
    event_kinds: list[str],
) -> ReportSection:
    """Article 12(1) - Registration of events.

    Automatic logging of events throughout the high-risk AI system lifecycle.
    Maps to: audit trail event count, date range, completeness.
    """
    status: SectionStatus = _status_from_count(total_events, partial_threshold=10)

    data: list[dict[str, Any]] = [
        {"metric": "total_events", "value": total_events},
        {"metric": "date_range_start", "value": date_range_start},
        {"metric": "date_range_end", "value": date_range_end},
        {"metric": "event_kinds_observed", "value": event_kinds},
    ]

    return ReportSection(
        title="Registration of Events",
        description=(
            "EU AI Act Article 12 requires automatic logging of events "
            "throughout the AI system lifecycle. This section shows audit "
            "trail completeness for the queried scope."
        ),
        data=data,
        status=status,
    )


def section_duration_of_use(
    *,
    sessions: list[dict[str, Any]],
) -> ReportSection:
    """Article 12(2) - Duration of use.

    Logging must cover the period of each use. Maps to session start/end times.
    """
    status: SectionStatus = _status_from_count(len(sessions))

    data: list[dict[str, Any]] = [
        {"metric": "total_sessions", "value": len(sessions)},
        {"metric": "sessions", "value": sessions},
    ]

    return ReportSection(
        title="Duration of Use",
        description=(
            "Article 12 requires logging the period of each use of the AI system. "
            "Sessions with both start and end events demonstrate complete lifecycle tracking."
        ),
        data=data,
        status=status,
    )


def section_reference_database(
    *,
    agent_ids: list[str],
    tools: list[str],
    models: list[str],
) -> ReportSection:
    """Article 12(3) - Reference database.

    Identification of the input data against which the system was tested.
    Maps to: agent registry, tool list, model list.
    """
    has_agents = len(agent_ids) > 0
    has_tools = len(tools) > 0
    has_models = len(models) > 0
    all_present = has_agents and has_tools and has_models

    status: SectionStatus
    if all_present:
        status = "compliant"
    elif has_agents or has_tools or has_models:
        status = "partial"
    else:
        status = "non_compliant"

    data: list[dict[str, Any]] = [
        {"metric": "agent_ids", "value": agent_ids},
        {"metric": "tools_observed", "value": tools},
        {"metric": "models_observed", "value": models},
    ]

    return ReportSection(
        title="Reference Database",
        description=(
            "Article 12 requires identification of agents, tools, and models involved. "
            "This section lists all entities observed in the audit trail."
        ),
        data=data,
        status=status,
    )


def section_input_data(
    *,
    events_with_input_hash: int,
    total_events: int,
) -> ReportSection:
    """Article 12(4) - Input data.

    Input data used by the system. We hash inputs rather than store raw data
    (privacy-preserving approach). Presence of input_hash demonstrates traceability.
    """
    ratio = events_with_input_hash / total_events if total_events > 0 else 0.0

    status: SectionStatus
    if total_events == 0:
        status = "non_compliant"
    elif ratio >= 0.8:
        status = "compliant"
    elif ratio > 0.0:
        status = "partial"
    else:
        status = "non_compliant"

    data: list[dict[str, Any]] = [
        {"metric": "events_with_input_hash", "value": events_with_input_hash},
        {"metric": "total_events", "value": total_events},
        {"metric": "coverage_ratio", "value": round(ratio, 4)},
        {
            "metric": "note",
            "value": (
                "Input data is privacy-preserving: hashed, not stored raw. "
                "The hash enables traceability without retaining sensitive content."
            ),
        },
    ]

    return ReportSection(
        title="Input Data",
        description=(
            "Article 12 requires logging of input data. This SDK uses "
            "cryptographic hashing (privacy-preserving) to record input "
            "traceability without storing raw content."
        ),
        data=data,
        status=status,
    )


def section_functioning(
    *,
    scope_policies_count: int,
    budget_policies_count: int,
    hitl_gates_count: int,
) -> ReportSection:
    """Article 12(5) - Functioning of the system.

    Verification of the functioning of the monitoring system.
    Maps to: active scope policies, budget policies, HITL gates.
    """
    total = scope_policies_count + budget_policies_count + hitl_gates_count

    status: SectionStatus
    if total >= 3:
        status = "compliant"
    elif total > 0:
        status = "partial"
    else:
        status = "non_compliant"

    data: list[dict[str, Any]] = [
        {"metric": "scope_policies_active", "value": scope_policies_count},
        {"metric": "budget_policies_active", "value": budget_policies_count},
        {"metric": "hitl_gates_active", "value": hitl_gates_count},
    ]

    return ReportSection(
        title="Functioning of the System",
        description=(
            "Article 12 requires verification that monitoring mechanisms function "
            "correctly. This section summarizes active enforcement policies "
            "(scope, budget, human-in-the-loop gates)."
        ),
        data=data,
        status=status,
    )


def section_human_oversight(
    *,
    approval_requested: int,
    approval_granted: int,
    approval_denied: int,
) -> ReportSection:
    """Article 12(6) - Human oversight measures.

    Measures of human oversight, including approval/denial ratios.
    Maps to: HITL gate count, approval/denial ratio.
    """
    total = approval_requested

    status: SectionStatus
    if total > 0 and (approval_granted + approval_denied) == total:
        status = "compliant"
    elif total > 0:
        status = "partial"
    else:
        status = "non_compliant"

    data: list[dict[str, Any]] = [
        {"metric": "approval_requested", "value": approval_requested},
        {"metric": "approval_granted", "value": approval_granted},
        {"metric": "approval_denied", "value": approval_denied},
        {
            "metric": "approval_rate",
            "value": (
                round(approval_granted / total, 4) if total > 0 else None
            ),
        },
    ]

    return ReportSection(
        title="Human Oversight Measures",
        description=(
            "Article 12 requires logging of human oversight actions. "
            "This section shows HITL gate activity: requests, approvals, and denials."
        ),
        data=data,
        status=status,
    )


def section_post_market_monitoring(
    *,
    scope_violations: int,
    budget_violations: int,
    loop_violations: int,
    chain_integrity_verified: bool,
) -> ReportSection:
    """Article 12(7) - Post-market monitoring.

    Relevant for post-market monitoring. Maps to violation counts and
    chain integrity status.
    """
    total_violations = scope_violations + budget_violations + loop_violations

    status: SectionStatus
    if chain_integrity_verified and total_violations == 0:
        status = "compliant"
    elif chain_integrity_verified:
        status = "partial"
    else:
        status = "non_compliant"

    data: list[dict[str, Any]] = [
        {"metric": "scope_violations", "value": scope_violations},
        {"metric": "budget_violations", "value": budget_violations},
        {"metric": "loop_violations", "value": loop_violations},
        {"metric": "total_violations", "value": total_violations},
        {"metric": "chain_integrity_verified", "value": chain_integrity_verified},
    ]

    return ReportSection(
        title="Post-Market Monitoring",
        description=(
            "Article 12 requires logging relevant to post-market monitoring. "
            "This section shows policy violations and HMAC chain integrity status."
        ),
        data=data,
        status=status,
    )
