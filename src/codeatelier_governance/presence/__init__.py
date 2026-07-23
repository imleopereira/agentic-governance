"""Agent presence tracking.

Public API:
    AgentStatus      — 'live', 'idle', 'unresponsive'
    PresenceModule   — exposed via sdk.presence
    AgentHaltedError — raised by sdk.presence.assert_not_halted() and by
                       scope/cost/gates checks when the operator has halted
                       an agent (v0.6 rename of the v0.5.4 kill switch).
    HaltPersistenceError — raised by sdk.presence.halt() when the halt could
                       not be persisted (denied/failed write), so the kill
                       switch did NOT engage.

Backward-compat (removed in v0.7):
    AgentKilledError — alias of AgentHaltedError.
"""
from .errors import AgentHaltedError, AgentKilledError, HaltPersistenceError
from .models import AgentStatus
from .module import PresenceModule

__all__ = [
    "AgentHaltedError",
    "AgentKilledError",  # deprecated alias; removed in v0.7
    "AgentStatus",
    "HaltPersistenceError",
    "PresenceModule",
]
