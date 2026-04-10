"""Agent presence tracking.

Public API:
    AgentStatus    — 'live', 'idle', 'unresponsive'
    PresenceModule — exposed via sdk.presence
"""
from .models import AgentStatus
from .module import PresenceModule

__all__ = [
    "AgentStatus",
    "PresenceModule",
]
