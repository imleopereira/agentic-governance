"""Pydantic response-model surface for the governance console backend.

Models in this package are the ONLY acceptable return shapes for authenticated
`/api` endpoints. Every model declares ``extra="forbid"`` so internal state
(pool counts, asyncpg versions, stack frames) can never leak through a
stray ``dict`` passthrough. A collection-time lint in
``tests/console/test_response_models_forbid_extra.py`` enforces the rule.
"""
from __future__ import annotations

from .responses import (
    AgentPoliciesResponse,
    AgentPresenceResponse,
    AgentPresenceRow,
    BatchApproveFailure,
    BatchApproveResponse,
    EventStatsResponse,
    GateAgentPresence,
    GateClaimResponse,
    GateContextResponse,
    GateEscalateResponse,
    GateRecentEvent,
    PolicyListResponse,
    PolicyRow,
    SessionRevokeResponse,
)

__all__ = [
    "AgentPoliciesResponse",
    "AgentPresenceResponse",
    "AgentPresenceRow",
    "BatchApproveFailure",
    "BatchApproveResponse",
    "EventStatsResponse",
    "GateAgentPresence",
    "GateClaimResponse",
    "GateContextResponse",
    "GateEscalateResponse",
    "GateRecentEvent",
    "PolicyListResponse",
    "PolicyRow",
    "SessionRevokeResponse",
]
