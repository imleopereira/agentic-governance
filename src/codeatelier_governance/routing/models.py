"""Pydantic models for routing policies.

Design notes:
    * Advisory only — routing never blocks a call, it only suggests a model.
    * Two strategies: ``cost_aware`` (budget-driven tier selection) and
      ``rules`` (explicit model remapping).
    * Policies are frozen at construction to prevent runtime mutation.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RoutingPolicy(BaseModel):
    """Declarative routing policy for a single agent.

    ``cost_aware`` strategy:
        Routes to cheaper model tiers when remaining budget drops below the
        configured thresholds. Tier order (cheapest first): cheap_model →
        mid_model → expensive_model.

    ``rules`` strategy:
        Explicitly remaps a requested model to a target model.  Useful for
        enforcing that a team always uses a specific version, or that a
        low-trust agent never gets access to a more capable model.

    Example (cost_aware)::

        RoutingPolicy(
            agent_id="billing-agent",
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            mid_model="claude-sonnet-4-6",
            expensive_model="claude-opus-4-6",
            min_session_usd_for_expensive=0.10,
            min_daily_usd_for_expensive=1.00,
        )

    Example (rules)::

        RoutingPolicy(
            agent_id="customer-agent",
            strategy="rules",
            model_rules={"claude-opus-4-6": "claude-sonnet-4-6"},
        )
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1, max_length=256)
    strategy: Literal["cost_aware", "rules"]

    # Model tiers — at least one must be set for cost_aware
    cheap_model: str | None = Field(default=None, min_length=1, max_length=128)
    mid_model: str | None = Field(default=None, min_length=1, max_length=128)
    expensive_model: str | None = Field(default=None, min_length=1, max_length=128)

    # cost_aware thresholds: minimum budget remaining (USD) to keep using the
    # expensive/mid model.  If remaining budget drops below this, route down.
    min_session_usd_for_expensive: float | None = Field(default=None, ge=0.0)
    min_daily_usd_for_expensive: float | None = Field(default=None, ge=0.0)

    # rules strategy: explicit map from requested_model -> target_model
    # e.g. {"claude-opus-4-6": "claude-sonnet-4-6"}
    model_rules: dict[str, str] = Field(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        """Validate strategy-specific constraints."""
        if self.strategy == "cost_aware":
            if not any([self.cheap_model, self.mid_model, self.expensive_model]):
                raise ValueError(
                    "cost_aware strategy requires at least one of: "
                    "cheap_model, mid_model, expensive_model"
                )
            # Tier models must be distinct when more than one is specified
            tiers = [
                m for m in [self.cheap_model, self.mid_model, self.expensive_model]
                if m is not None
            ]
            if len(tiers) != len(set(tiers)):
                raise ValueError(
                    "cost_aware strategy: cheap_model, mid_model, expensive_model "
                    "must be distinct"
                )
        if self.strategy == "rules" and not self.model_rules:
            raise ValueError("rules strategy requires model_rules to be non-empty")
        # Validate model_rules key/value lengths
        for k, v in self.model_rules.items():
            if not (1 <= len(k) <= 128):
                raise ValueError(
                    f"model_rules key {k!r} must be between 1 and 128 characters"
                )
            if not (1 <= len(v) <= 128):
                raise ValueError(
                    f"model_rules value {v!r} must be between 1 and 128 characters"
                )
