"""Agent presence tracking.

Public API:
    AgentStatus      — 'live', 'idle', 'unresponsive'
    PresenceModule   — exposed via sdk.presence
    AgentKilledError — raised by sdk.presence.assert_alive() and by
                       scope/cost/gates checks when the operator has killed
                       an agent via the console kill switch (v0.5.4)
"""
from .errors import AgentKilledError
from .models import AgentStatus
from .module import PresenceModule

__all__ = [
    "AgentKilledError",
    "AgentStatus",
    "PresenceModule",
]
