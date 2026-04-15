"""Presence/kill-switch enforcement exceptions.

Public API:
    AgentKilledError — raised when an action is attempted by an agent that
                       an operator has killed via the console kill switch.
"""
from __future__ import annotations


class AgentKilledError(RuntimeError):
    """Raised when an agent action is blocked because the agent has been killed.

    The kill switch is set via the console (`POST /api/agents/{agent_id}/kill`)
    or the SDK admin API. Once an agent is killed:

      * scope.check() raises this for every subsequent call
      * cost.check_budget() raises this
      * gates.check() raises this
      * any wrapped LLM client (wrap_anthropic / wrap_openai) raises this
        before the call hits the network

    The only way to "un-kill" an agent is for an operator to clear the
    kill marker in `governance_agent_presence.metadata_json` (e.g. via SQL
    or via a future console "Restore" action). This exception is intentionally
    NOT recoverable from inside the host application — kill is a human-issued,
    human-resolved control.

    Attributes:
        agent_id: The agent that was killed.
        killed_by: The operator user_id from the kill request, if known.
        killed_at: ISO timestamp from the kill request, if known.
        reason: The reason string from the kill request, if known.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        killed_by: str | None = None,
        killed_at: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.killed_by = killed_by
        self.killed_at = killed_at
        self.reason = reason
        parts = [f"Agent {agent_id!r} has been killed by an operator"]
        if killed_by:
            parts.append(f"(by {killed_by})")
        if killed_at:
            parts.append(f"at {killed_at}")
        if reason:
            parts.append(f": {reason}")
        parts.append(
            ". All scope, cost, gate, and LLM-call enforcement is now fail-closed "
            "for this agent. Clear the kill marker via the console or SQL to restore."
        )
        super().__init__(" ".join(parts))
