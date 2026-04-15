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
from typing import Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Strict primitive-only value type for metadata fields.
# ``dict[str, Any]`` is explicitly forbidden; we only allow JSON scalars.
MetadataValue = Union[str, int, float, bool, None]

_STRICT = ConfigDict(extra="forbid", strict=True)


class StrictResponse(BaseModel):
    """Base class for every console response model.

    Enforces ``extra='forbid'`` and ``strict=True`` at class-creation time
    via ``__init_subclass__``. This closes a gap that the collection-time
    conftest lint cannot cover: a rogue subclass defined anywhere in the
    codebase (not just in this module) that silently relaxes the config is
    rejected the moment Python evaluates the ``class`` statement.

    The conftest lint still runs as a belt-and-suspenders check.
    """

    model_config = _STRICT

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cfg = getattr(cls, "model_config", {}) or {}
        extra = cfg.get("extra") if isinstance(cfg, dict) else None
        strict = cfg.get("strict") if isinstance(cfg, dict) else None
        if extra != "forbid" or strict is not True:
            raise TypeError(
                f"{cls.__name__} must declare "
                f"model_config=ConfigDict(extra='forbid', strict=True); "
                f"got extra={extra!r}, strict={strict!r}. Response models "
                f"leak internal state when extras are permitted."
            )


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------
class PolicyRow(StrictResponse):
    """Single row from governance_policies.

    DA Wave 4 blocker fix (F1): the four scope list fields are hoisted to
    typed top-level attributes. The ``policy`` dict is strictly scalar-only
    (``MetadataValue`` = JSON scalars); any attempt to stuff a list into it
    is rejected at serialization time. Backend handlers are responsible
    for lifting ``allowed_tools`` / ``hidden_tools`` / ``allowed_apis`` /
    ``allowed_models`` off the raw policy JSON before instantiating this
    model.
    """

    model_config = _STRICT

    agent_id: str
    policy_type: str
    policy: dict[str, MetadataValue] = Field(
        description=(
            "Parsed policy JSON. Values restricted to JSON scalars — list "
            "fields like allowed_tools are exposed as typed top-level "
            "attributes instead and MUST NOT appear in this dict."
        ),
    )
    #: Scope: tools this agent is permitted to call. ``None`` when the
    #: row is not a scope policy or the backend did not populate it.
    allowed_tools: list[str] | None = None
    #: Scope: tools suppressed from the UI for this agent.
    hidden_tools: list[str] | None = None
    #: Scope: HTTP hosts / APIs this agent may call.
    allowed_apis: list[str] | None = None
    #: Scope: LLM model IDs this agent may invoke.
    allowed_models: list[str] | None = None
    updated_at: datetime | None = None


class PolicyListResponse(StrictResponse):
    """GET /api/policies — all policies, sorted by (agent_id, policy_type)."""

    model_config = _STRICT

    policies: list[PolicyRow]


class AgentPoliciesResponse(StrictResponse):
    """GET /api/policies/{agent_id} — scope + budget for one agent."""

    model_config = _STRICT

    agent_id: str
    policies: list[PolicyRow]


# ---------------------------------------------------------------------------
# Agent presence
# ---------------------------------------------------------------------------
class AgentPresenceRow(StrictResponse):
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


class AgentPresenceResponse(StrictResponse):
    """GET /api/agents/presence — all agents with presence status."""

    model_config = _STRICT

    agents: list[AgentPresenceRow]


# ---------------------------------------------------------------------------
# Event stats
# ---------------------------------------------------------------------------
class EventStatsResponse(StrictResponse):
    """GET /api/events/stats — rolling counts for the last hour / 5 min."""

    model_config = _STRICT

    total_last_hour: int = Field(ge=0)
    per_kind_counts: dict[str, int]
    events_per_minute: float = Field(ge=0.0)


# ---------------------------------------------------------------------------
# Gate context
# ---------------------------------------------------------------------------
class GateAgentPresence(StrictResponse):
    """Presence snapshot embedded in GET /api/gates/{id}/context."""

    model_config = _STRICT

    status: str
    last_heartbeat: datetime | None = None


class GateRecentEvent(StrictResponse):
    """One row in the recent-agent-events list on gate context."""

    model_config = _STRICT

    kind: str
    created_at: datetime | None = None


class GateContextResponse(StrictResponse):
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
class GateClaimResponse(StrictResponse):
    """POST /api/gates/{request_id}/claim — reviewer claim response."""

    model_config = _STRICT

    ok: bool
    request_id: str
    reviewer_id: str
    reviewing_since: datetime


# ---------------------------------------------------------------------------
# Gate escalate
# ---------------------------------------------------------------------------
class GateEscalateResponse(StrictResponse):
    """POST /api/gates/{request_id}/escalate — escalation response."""

    model_config = _STRICT

    ok: bool
    request_id: str
    escalated_to: str
    escalated_at: datetime


# ---------------------------------------------------------------------------
# Batch approve
# ---------------------------------------------------------------------------
class BatchApproveFailure(StrictResponse):
    """One failed item inside the batch-approve response."""

    model_config = _STRICT

    request_id: str
    reason: str


class BatchApproveResponse(StrictResponse):
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
class SessionRevokeResponse(StrictResponse):
    """DELETE /api/auth/sessions/{session_id} — revoke a session."""

    model_config = _STRICT

    ok: bool
    session_id: str


# ---------------------------------------------------------------------------
# Audit event detail (F2 P0 SSE hydration endpoint)
# ---------------------------------------------------------------------------
class AuditEventView(StrictResponse):
    """GET /api/events/{event_id} — single audit-event row.

    Used by the console SSE hydration path: the NOTIFY trigger emits a
    minimal envelope (event_id, agent_id, kind, chain_seq, created_at) to
    keep WAL small, and the frontend calls this endpoint on first access
    to lazy-hydrate the remaining fields (model / metadata / hmac_value /
    prev_hash).

    Metadata values are restricted to JSON scalars to match the rest of
    the strict response surface, and the endpoint applies the same
    ``_redact_metadata`` layer as the list endpoint before serialization.
    """

    model_config = _STRICT

    event_id: str
    chain_seq: int
    agent_id: str
    kind: str
    model: str | None = None
    tool: str | None = None
    request_id: str | None = None
    metadata: dict[str, MetadataValue] = Field(
        default_factory=dict,
        description="Redacted metadata. Values restricted to JSON scalars.",
    )
    hmac_value: str | None = None
    prev_hash: str | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# F9 Wrapper Coverage
# ---------------------------------------------------------------------------
class WrapperCoverageAgentEntry(StrictResponse):
    """One row in the ``by_agent`` list on ``GET /api/coverage``."""

    model_config = _STRICT

    agent_id: str
    provider: str
    active: bool
    last_seen_at: datetime | None = None


class WrapperCoverageView(StrictResponse):
    """GET /api/coverage — F9 wrapper coverage registry view.

    Shape matches ``decisions/2026-04-15-f9-wrapper-coverage-design.md``
    section 11. ``coverage_pct`` may be ``None`` when no scope policies
    are declared or the registry is disabled — always paired with
    ``coverage_pct_reason`` so consumers can disambiguate.
    """

    model_config = _STRICT

    as_of: datetime
    active_wrappers: int = Field(ge=0)
    total_wrappers: int = Field(ge=0)
    coverage_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    coverage_pct_reason: str | None = None
    by_agent: list[WrapperCoverageAgentEntry]
    unwrapped_agents_seen_in_audit: list[str]
    active_window_days: int = Field(ge=1)


# ---------------------------------------------------------------------------
# F7 Governance Health
# ---------------------------------------------------------------------------
class GovernanceHealthView(StrictResponse):
    """GET /health/governance — authenticated response shape.

    Per F7 (v0.6 PRD), this endpoint has two response shapes:

    * Unauthenticated callers receive only ``{"status": "ok"}`` to avoid
      leaking load/latency patterns to anonymous probes (K8s liveness
      compatibility). That minimal shape is serialized as a plain dict and
      does NOT go through this model.
    * Authenticated callers (session cookie or dev mode) receive the full
      view below, including chain-integrity signals and missing-key
      fingerprints surfaced by F6 Track B.

    The ``chain_keys_unresolved`` list is the LOUD signal that F6 Track B
    deferred to F7: any HMAC key fingerprint referenced by audit events
    that cannot currently be resolved to a verification key. A non-empty
    list with ``chain_integrity_status`` still ``verified`` means the
    chain segment signed by the missing key is skipped rather than
    rejected, and operators MUST rotate or restore the key.
    """

    model_config = _STRICT

    status: str
    db_reachable: bool
    last_chain_verify_ts: datetime | None = None
    append_only_grants_ok: bool
    audit_write_p50_ms: float | None = Field(default=None, ge=0.0)
    audit_write_p95_ms: float | None = Field(default=None, ge=0.0)
    chain_integrity_status: str  # verified | unverified | degraded | halted
    chain_keys_resolved: list[str]
    chain_keys_unresolved: list[str]


# ---------------------------------------------------------------------------
# F4 Compliance Console Surface
# ---------------------------------------------------------------------------
ComplianceChainStatus = Literal["verified", "unverified", "degraded", "halted"]
CoveragePctReason = Literal[
    "no_scope_policies_registered",
    "registry_disabled",
    "ok",
]


class ComplianceReportView(StrictResponse):
    """GET /api/compliance/report — Article 12 evidence summary view.

    The full ``ComplianceReport`` Pydantic carries the section-level
    detail; this surface view exposes only the top-level fields the
    frontend renders in the compliance page and in the persistent header
    pill. Field names align 1:1 with the backing report so an auditor
    can cross-reference the JSON directly against
    ``codeatelier_governance.compliance.models.ComplianceReport``.

    DA blocker fix (F4): the ``chain_verified_from_seq`` /
    ``chain_verified_to_seq`` fields make the verification window
    explicit. The default window is the last 1000 events — without this
    pair a reader cannot tell whether the verified status applies to
    the whole chain or a slice.
    """

    model_config = _STRICT

    report_id: UUID
    generated_at: datetime
    chain_integrity_status: ComplianceChainStatus
    chain_verified_from_seq: int | None = Field(default=None, ge=0)
    chain_verified_to_seq: int | None = Field(default=None, ge=0)
    #: DA Wave 4: ``True`` when the rotation-aware verifier path was taken
    #: (F6 Track B). When ``False``, any sibling ``unresolved_fingerprints``
    #: list is ALWAYS empty and MUST NOT be interpreted as "all keys
    #: verified successfully" — it is simply not tracked on this path.
    #: Defaults to ``False`` in v0.6 because the rotation-aware path is
    #: not yet wired into the console handlers.
    rotation_aware: bool = False
    coverage_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    coverage_pct_reason: CoveragePctReason | None = None
    coverage_caveat: str | None = None
    total_events_audited: int = Field(ge=0)
    total_agents: int = Field(ge=0)


class VerifyChainResponse(StrictResponse):
    """POST /api/compliance/verify-chain — on-demand chain re-verification.

    Returns a fresh HMAC chain verification over the caller-supplied
    window (or the last 1000 events by default).

    ``unresolved_fingerprints`` is populated ONLY when ``rotation_aware``
    is ``True`` (the F6 Track B rotation-aware verifier path). On the
    default single-key path, ``rotation_aware`` is ``False`` and
    ``unresolved_fingerprints`` is ALWAYS empty — an empty list is NOT
    a verification signal on that path and MUST NOT be interpreted as
    "all keys verified successfully". DA Wave 4 fix: the new
    ``rotation_aware`` flag makes the distinction explicit so auditors
    reading the JSON cannot misread ``[]`` as a green light.
    """

    model_config = _STRICT

    chain_integrity_status: ComplianceChainStatus
    from_seq: int | None = Field(default=None, ge=0)
    to_seq: int | None = Field(default=None, ge=0)
    verified_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    #: See class docstring. Defaults to ``False`` in v0.6 because the
    #: rotation-aware verifier path is not yet wired into the console.
    rotation_aware: bool = False
    unresolved_fingerprints: list[str]
    verified_at_utc: datetime
