"""Loop detection exceptions."""
from __future__ import annotations

from ..errors import GovernanceError


class LoopError(GovernanceError):
    """Base class for loop module errors."""


class LoopDetected(LoopError):
    """Raised when an agent is detected in a tool-call loop."""
