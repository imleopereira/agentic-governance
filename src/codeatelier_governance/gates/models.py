"""Pydantic models for HITL approval requests and tokens."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_KIND_LEN = 128
MAX_AGENT_ID_LEN = 256


class ApprovalRequest(BaseModel):
    """A pending approval request — handed back to callers to display + resolve."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    request_id: UUID
    agent_id: str = Field(min_length=1, max_length=MAX_AGENT_ID_LEN)
    kind: str = Field(min_length=1, max_length=MAX_KIND_LEN)
    action_hash: str = Field(min_length=64, max_length=128)
    expires_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    token: str = Field(min_length=64)
