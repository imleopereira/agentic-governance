"""Pydantic models for behavioral contracts.

Design notes:
    * Pre/post conditions are declarative — check names map to built-in
      implementations or registered custom callables.
    * Contracts are frozen at construction — the agent's execution context
      cannot mutate its own contract.
    * Field validations enforce size caps to prevent DoS.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_AGENT_ID_LEN = 256
MAX_TOOL_NAME_LEN = 256
MAX_CONDITIONS = 20
VALID_PRE_CHECKS = frozenset({"hitl_approved", "budget_available", "scope_allowed", "custom"})
VALID_POST_CHECKS = frozenset({"audit_logged", "custom"})


class PreCondition(BaseModel):
    """A pre-condition that must pass before a tool call is allowed."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
    )

    check: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1024)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("check")
    @classmethod
    def _validate_check(cls, v: str) -> str:
        if v not in VALID_PRE_CHECKS:
            raise ValueError(
                f"pre-condition check must be one of {sorted(VALID_PRE_CHECKS)}, "
                f"got {v!r}"
            )
        return v


class PostCondition(BaseModel):
    """A post-condition that must hold after a tool call completes."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
    )

    check: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1024)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("check")
    @classmethod
    def _validate_check(cls, v: str) -> str:
        if v not in VALID_POST_CHECKS:
            raise ValueError(
                f"post-condition check must be one of {sorted(VALID_POST_CHECKS)}, "
                f"got {v!r}"
            )
        return v


class Contract(BaseModel):
    """Behavioral contract binding pre/post conditions to a tool call.

    Example:
        Contract(
            agent_id="billing-agent",
            tool="charge_customer",
            pre=[
                PreCondition(check="hitl_approved", message="Charges require human approval"),
                PreCondition(check="budget_available", message="Budget must not be exceeded"),
            ],
            post=[
                PostCondition(check="audit_logged", message="Charge must be audit-logged"),
            ],
        )
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
    )

    agent_id: str = Field(min_length=1, max_length=MAX_AGENT_ID_LEN)
    tool: str = Field(min_length=1, max_length=MAX_TOOL_NAME_LEN)
    pre: list[PreCondition] = Field(default_factory=list)
    post: list[PostCondition] = Field(default_factory=list)

    def model_post_init(self, __context: object) -> None:
        if len(self.pre) > MAX_CONDITIONS:
            raise ValueError(
                f"contract: pre has {len(self.pre)} conditions "
                f"(max {MAX_CONDITIONS}). Fix: reduce the number of pre-conditions."
            )
        if len(self.post) > MAX_CONDITIONS:
            raise ValueError(
                f"contract: post has {len(self.post)} conditions "
                f"(max {MAX_CONDITIONS}). Fix: reduce the number of post-conditions."
            )
