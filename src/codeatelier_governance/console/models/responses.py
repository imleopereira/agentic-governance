"""Typed response models for the governance console backend.

Every model in this module is a ``pydantic.BaseModel`` with:

* ``model_config = ConfigDict(extra="forbid", strict=True)`` — unknown fields
  raise ``ValidationError`` instead of silently passing through.
* Explicit, typed fields only — no ``Any``, no free-form ``dict[str, Any]``.

Shapes are deliberately aligned to what the current endpoints in
``codeatelier_governance.console.app`` return today (as of v0.5.4). The models
are NOT yet wired into the FastAPI decorators — that is F3's job in v0.6.
This module only defines the contract and lands the lint rule that enforces
``extra="forbid"`` on every response shape going forward.

Threat model: a stray ``dict`` passthrough leaks internal state
(DB pool counts, asyncpg versions, pytest tracebacks, filesystem paths) to
every authenticated caller. The class-level ``extra="forbid"`` closes that
class of bugs at the serialization boundary. The collection-time lint in
``tests/console/test_response_models_forbid_extra.py`` prevents regressions.
"""
from __future__ import annotations

from datetime import datetime
from typing import Union

from pydantic import BaseModel, ConfigDict, Field

# Strict primitive-only value type for metadata fields.
# ``dict[str, Any]`` is explicitly forbidden; we only allow JSON scalars.
MetadataValue = Union[str, int, float, bool, None]

_STRICT = ConfigDict(extra="forbid", strict=True)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------
class PolicyRow(BaseModel):
    """Single row from governance_policies."""

    model_config = _STRICT

    agent_id: str
    policy_type: str
    policy: dict[str, MetadataValue] = Field(
        description="Parsed policy JSON. Values restricted to JSON scalars.",
    )
    updated_at: datetime | None = None


class PolicyListResponse(BaseModel):
    """GET /api/policies — all policies, sorted by (agent_id, policy_type)."""

    model_config = _STRICT

    policies: list[PolicyRow]


class AgentPoliciesResponse(BaseModel):
    """GET /api/policies/{agent_id} — scope + budget for one agent."""

    model_config = _STRICT

    agent_id: str
    policies: list[PolicyRow]


# ---------------------------------------------------------------------------
# Agent presence
# ---------------------------------------------------------------------------
class AgentPresenceRow(BaseModel):
    """Single row from governance_agent_presence."""

    model_config = _STRICT

    agent_id: str
    status: str
    last_heartbeat: datetime | None = None
    started_at: datetime | None = None
    metadata: dict[str, MetadataValue] = Field(
        default_factory=dict,
        description="Agent metadata. Values restricted to JSON scalars.",
    )


class AgentPresenceResponse(BaseModel):
    """GET /api/agents/presence — all agents with presence status."""

    model_config = _STRICT

    agents: list[AgentPresenceRow]


# ---------------------------------------------------------------------------
# Event stats
# ---------------------------------------------------------------------------
class EventStatsResponse(BaseModel):
    """GET /api/events/stats — rolling counts for the last hour / 5 min."""

    model_config = _STRICT

    total_last_hour: int = Field(ge=0)
    per_kind_counts: dict[str, int]
    events_per_minute: float = Field(ge=0.0)


# ---------------------------------------------------------------------------
# Gate context
# ---------------------------------------------------------------------------
class GateAgentPresence(BaseModel):
    """Presence snapshot embedded in GET /api/gates/{id}/context."""

    model_config = _STRICT

    status: str
    last_heartbeat: datetime | None = None


class GateRecentEvent(BaseModel):
    """One row in the recent-agent-events list on gate context."""

    model_config = _STRICT

    kind: str
    created_at: datetime | None = None


class GateContextResponse(BaseModel):
    """GET /api/gates/{request_id}/context — rich approval context."""

    model_config = _STRICT

    request_id: str
    agent_id: str
    kind: str
    action_hash: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: str | None = None
    reviewer_id: str | None = None
    reviewing_since: datetime | None = None
    rationale: str | None = None
    payload: dict[str, MetadataValue] = Field(
        default_factory=dict,
        description="Redacted payload. Values restricted to JSON scalars.",
    )
    risk: str
    agent_presence: GateAgentPresence | None = None
    recent_agent_events: list[GateRecentEvent]
    agent_cost_today_usd: float | None = None


# ---------------------------------------------------------------------------
# Gate claim
# ---------------------------------------------------------------------------
class GateClaimResponse(BaseModel):
    """POST /api/gates/{request_id}/claim — reviewer claim response."""

    model_config = _STRICT

    ok: bool
    request_id: str
    reviewer_id: str
    reviewing_since: datetime


# ---------------------------------------------------------------------------
# Gate escalate
# ---------------------------------------------------------------------------
class GateEscalateResponse(BaseModel):
    """POST /api/gates/{request_id}/escalate — escalation response."""

    model_config = _STRICT

    ok: bool
    request_id: str
    escalated_to: str
    escalated_at: datetime


# ---------------------------------------------------------------------------
# Batch approve
# ---------------------------------------------------------------------------
class BatchApproveFailure(BaseModel):
    """One failed item inside the batch-approve response."""

    model_config = _STRICT

    request_id: str
    reason: str


class BatchApproveResponse(BaseModel):
    """POST /api/gates/batch-approve — batch approval response."""

    model_config = _STRICT

    ok: bool
    approved: list[str]
    failed: list[BatchApproveFailure]
    approved_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Session revoke
# ---------------------------------------------------------------------------
class SessionRevokeResponse(BaseModel):
    """DELETE /api/auth/sessions/{session_id} — revoke a session."""

    model_config = _STRICT

    ok: bool
    session_id: str
