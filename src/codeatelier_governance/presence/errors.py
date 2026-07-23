"""Presence/halt-switch enforcement exceptions.

Public API:
    AgentHaltedError — raised when an action is attempted by an agent that
                       an operator has halted (see PresenceModule.halt).
    HaltPersistenceError — raised when an operator halt could not be persisted
                       (e.g. the write was denied), so the kill switch did NOT
                       engage.

Backward-compat alias (removed in v0.7):
    AgentKilledError — alias for AgentHaltedError. Existing v0.5.x imports
                       continue to work unchanged for one release.
"""
from __future__ import annotations


class AgentHaltedError(RuntimeError):
    """Raised when an agent action is blocked because the agent has been halted.

    The halt switch is set via :meth:`PresenceModule.halt` (an operator or
    admin action, ideally through a privileged connection). Once an agent is
    halted:

      * scope.check() raises this for every subsequent call
      * cost.check_budget() raises this
      * gates.check() raises this
      * any wrapped LLM client (wrap_anthropic / wrap_openai) raises this
        before the call hits the network

    The only way to "un-halt" an agent is for an operator to clear the
    dedicated halt columns (`halted_by` / `halted_at` / `halt_reason`) on
    `governance_agent_presence`, via a privileged connection the agent role
    does not have. This exception is intentionally NOT recoverable from inside
    the host application — halt is a human-issued, human-resolved control.

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
            "for this agent. Clear the dedicated halt columns via a privileged "
            "connection to restore."
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


class HaltPersistenceError(RuntimeError):
    """Raised when an operator halt could not be persisted.

    :meth:`PresenceModule.halt` raises this instead of returning a false
    success when the halt write fails or is denied — for example, the
    app/agent role lacks UPDATE on the halt-marker columns because it runs
    under the self-unhalt REVOKE and no privileged halt connection was
    configured. A kill switch that silently no-ops is worse than one that
    errors loudly: this makes the operator aware the halt did NOT engage so
    they can retry through a privileged connection (see
    ``PresenceModule(halt_engine=...)`` / ``presence_halt_database_url``).
    """
