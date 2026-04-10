"""Pydantic models for budget policies and usage records.

Two scopes in v0.1:
    per_session_*       — caps tokens / USD spent within a single session
    per_agent_daily_*   — caps tokens / USD spent by an agent per UTC day

per-client scope is reserved for v0.2 when we ship multi-tenancy primitives.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_AGENT_ID_LEN = 256
MAX_USD_CAP = 1_000_000.0  # one million USD per scope — sanity ceiling
MAX_TOKEN_CAP = 1_000_000_000  # one billion tokens — sanity ceiling
MAX_SESSION_SECONDS = 86400  # 24 hours — sanity ceiling


class BudgetPolicy(BaseModel):
    """Declarative budget policy for a single agent.

    All caps are optional; passing None means "no limit on this dimension."
    Negative values are rejected at construction.
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
    )

    agent_id: str = Field(min_length=1, max_length=MAX_AGENT_ID_LEN)
    per_session_usd: float | None = Field(default=None, ge=0.0, le=MAX_USD_CAP)
    per_session_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKEN_CAP)
    per_agent_usd_daily: float | None = Field(default=None, ge=0.0, le=MAX_USD_CAP)
    per_agent_tokens_daily: int | None = Field(default=None, ge=0, le=MAX_TOKEN_CAP)
    per_session_seconds: int | None = Field(default=None, ge=0, le=MAX_SESSION_SECONDS)

    def model_post_init(self, __context: object) -> None:
        # At least one cap must be set, else the policy does nothing.
        any_set = any(
            v is not None
            for v in (
                self.per_session_usd,
                self.per_session_tokens,
                self.per_agent_usd_daily,
                self.per_agent_tokens_daily,
                self.per_session_seconds,
            )
        )
        if not any_set:
            raise ValueError(
                "BudgetPolicy must set at least one cap. "
                "Fix: pass per_session_usd= or per_agent_tokens_daily= etc."
            )


class BudgetSnapshot(BaseModel):
    """Read-only snapshot of current usage for an agent / session."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    agent_id: str
    session_usd_used: float
    session_tokens_used: int
    agent_daily_usd_used: float
    agent_daily_tokens_used: int
    session_usd_remaining: float | None
    session_tokens_remaining: int | None
    agent_daily_usd_remaining: float | None
    agent_daily_tokens_remaining: int | None
