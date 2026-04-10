"""Pydantic models for loop detection policies."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_AGENT_ID_LEN = 256


class LoopPolicy(BaseModel):
    """Declarative loop detection policy for a single agent.

    Detects when the same tool is called more than ``max_calls`` times
    within ``window_seconds`` for a given session. The ``action`` field
    controls whether to raise ``LoopDetected`` or just emit an audit event.
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
    )

    agent_id: str = Field(min_length=1, max_length=MAX_AGENT_ID_LEN)
    window_seconds: int = Field(default=60, ge=1, le=3600)
    max_calls: int = Field(default=5, ge=2, le=1000)
    action: Literal["log", "raise"] = "raise"
