"""Presence/halt-switch enforcement exceptions.

Public API:
    AgentHaltedError — raised when an action is attempted by an agent that
                       an operator has halted via the console halt switch.

Backward-compat alias (removed in v0.7):
    AgentKilledError — alias for AgentHaltedError. Existing v0.5.x imports
                       continue to work unchanged for one release.
"""
from __future__ import annotations


class AgentHaltedError(RuntimeError):
    """Raised when an agent action is blocked because the agent has been halted.

    The halt switch is set via the console (`POST /api/agents/{agent_id}/halt`)
    or the SDK admin API. Once an agent is halted:

      * scope.check() raises this for every subsequent call
      * cost.check_budget() raises this
      * gates.check() raises this
      * any wrapped LLM client (wrap_anthropic / wrap_openai) raises this
        before the call hits the network

    The only way to "un-halt" an agent is for an operator to clear the
    halt marker in `governance_agent_presence.metadata_json` (e.g. via SQL
    or via a future console "Restore" action). This exception is intentionally
    NOT recoverable from inside the host application — halt is a human-issued,
    human-resolved control.

    Attributes:
        agent_id: The agent that was halted.
        halted_by: The operator user_id from the halt request, if known.
        halted_at: ISO timestamp from the halt request, if known.
        reason: The reason string from the halt request, if known.

    Backward-compat: ``killed_by`` / ``killed_at`` are exposed as read-only
    ``@property`` aliases of ``halted_by`` / ``halted_at`` for v0.5.x callers.
    Removed in v0.7.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        halted_by: str | None = None,
        halted_at: str | None = None,
        reason: str | None = None,
        # Backward-compat keyword aliases (v0.5.x callers). Removed in v0.7.
        killed_by: str | None = None,
        killed_at: str | None = None,
    ) -> None:
        if halted_by is None and killed_by is not None:
            halted_by = killed_by
        if halted_at is None and killed_at is not None:
            halted_at = killed_at
        self.agent_id = agent_id
        self.halted_by = halted_by
        self.halted_at = halted_at
        self.reason = reason
        parts = [f"Agent {agent_id!r} has been halted by an operator"]
        if halted_by:
            parts.append(f"(by {halted_by})")
        if halted_at:
            parts.append(f"at {halted_at}")
        if reason:
            parts.append(f": {reason}")
        parts.append(
            ". All scope, cost, gate, and LLM-call enforcement is now fail-closed "
            "for this agent. Clear the halt marker via the console or SQL to restore."
        )
        super().__init__(" ".join(parts))

    # ------------------------------------------------------------------
    # Backward-compat read-only aliases (deprecated; removed in v0.7).
    # ------------------------------------------------------------------
    @property
    def killed_by(self) -> str | None:
        """Deprecated alias for ``halted_by``. Removed in v0.7."""
        return self.halted_by

    @property
    def killed_at(self) -> str | None:
        """Deprecated alias for ``halted_at``. Removed in v0.7."""
        return self.halted_at


# Backward-compat alias (deprecated; removed in v0.7). v0.5.x code that
# does `from codeatelier_governance.presence import AgentKilledError`
# continues to work unchanged. Note this is an identity alias — the two
# names refer to the same class, so `isinstance(err, AgentKilledError)`
# and `err.__class__ is AgentHaltedError` are both True.
AgentKilledError = AgentHaltedError
