"""Loop / anomaly detection for repeated tool calls.

Public API:
    LoopPolicy     — declarative loop detection policy per agent
    LoopDetected   — raised when a loop is detected (action='raise')
    LoopModule     — exposed via sdk.loop
"""
from .errors import LoopDetected, LoopError
from .models import LoopPolicy
from .module import LoopModule

__all__ = [
    "LoopDetected",
    "LoopError",
    "LoopModule",
    "LoopPolicy",
]
