"""Pydantic configuration for the F6 Ed25519 agent identity feature.

Constraint #6 (opt-in bootstrap): ``allow_bootstrap`` defaults to ``False``
because a first-runner-wins TOFU race is exploitable in multi-tenant
containers.  Operators must pre-provision keys out-of-band and flip the flag
explicitly to allow keygen.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class AgentIdentityConfig(BaseModel):
    """Config block for the agent identity subsystem.

    ``extra="forbid"`` per user memory: unknown keys are a typo, not a
    permissive-default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    """Master switch. When False, audit rows carry ``signature_status='unsigned'``
    and the signing code path is bypassed entirely."""

    key_source: Literal["file", "env", "ephemeral"] = "ephemeral"
    """Which keystore backend to load. ``ephemeral`` is the zero-config default
    so tests and local dev just work (constraint #3)."""

    allow_bootstrap: bool = False
    """Constraint #6: if False (default) and no key is found for this agent_id,
    startup raises ``KeyStoreError``. If True, a fresh keypair is generated and
    persisted via the configured backend."""

    key_uri: str | None = None
    """Optional backend-specific location override.

    - ``file`` backend: absolute path to the private key PEM file. When None,
      resolves to ``${XDG_DATA_HOME:-$HOME/.local/share}/codeatelier-governance/
      agents/<agent_id>.key``.
    - ``env`` backend: env var name (upper-snake). When None, resolves to
      ``CODEATELIER_AGENT_KEY_<AGENT_ID_UPPER_SNAKE>``.
    - ``ephemeral`` backend: ignored.
    """
