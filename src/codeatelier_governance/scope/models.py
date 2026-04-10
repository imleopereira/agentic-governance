"""Pydantic models for scope policies.

Design notes:
    * Whitelist only. No regex, no glob, no dynamic evaluation.
    * Exact string match OR explicit prefix match (pattern ending in ``*``).
    * Policies are frozen at construction — the agent's execution context
      cannot mutate its own policy.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_AGENT_ID_LEN = 256
MAX_TOOL_NAME_LEN = 256
MAX_API_PATTERN_LEN = 512
MAX_SET_SIZE = 1024


class ScopePolicy(BaseModel):
    """Declarative scope policy for a single agent.

    Example:
        ScopePolicy(
            agent_id="billing-agent",
            allowed_tools=frozenset({"read_invoice", "email_customer"}),
            allowed_apis=frozenset({
                "POST https://api.stripe.com/v1/charges",
                "GET https://api.stripe.com/v1/customers/*",  # prefix match
            }),
        )
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=False,
    )

    agent_id: str = Field(min_length=1, max_length=MAX_AGENT_ID_LEN)
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    allowed_apis: frozenset[str] = Field(default_factory=frozenset)
    hidden_tools: frozenset[str] = Field(default_factory=frozenset)

    def model_post_init(self, __context: object) -> None:
        if len(self.allowed_tools) > MAX_SET_SIZE:
            raise ValueError(
                f"scope policy: allowed_tools has {len(self.allowed_tools)} entries "
                f"(max {MAX_SET_SIZE}). Fix: split into multiple agents."
            )
        if len(self.allowed_apis) > MAX_SET_SIZE:
            raise ValueError(
                f"scope policy: allowed_apis has {len(self.allowed_apis)} entries "
                f"(max {MAX_SET_SIZE}). Fix: use prefix matches or split agents."
            )
        if len(self.hidden_tools) > MAX_SET_SIZE:
            raise ValueError(
                f"scope policy: hidden_tools has {len(self.hidden_tools)} entries "
                f"(max {MAX_SET_SIZE}). Fix: split into multiple agents."
            )
        for tool in self.allowed_tools:
            if len(tool) > MAX_TOOL_NAME_LEN or not tool:
                raise ValueError(
                    f"scope policy: tool name {tool!r} must be 1-{MAX_TOOL_NAME_LEN} chars"
                )
        for api in self.allowed_apis:
            if len(api) > MAX_API_PATTERN_LEN or not api:
                raise ValueError(
                    f"scope policy: api pattern {api!r} must be 1-{MAX_API_PATTERN_LEN} chars"
                )
        for tool in self.hidden_tools:
            if len(tool) > MAX_TOOL_NAME_LEN or not tool:
                raise ValueError(
                    f"scope policy: hidden tool name {tool!r} must be 1-{MAX_TOOL_NAME_LEN} chars"
                )
