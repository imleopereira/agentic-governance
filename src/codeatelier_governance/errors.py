"""Base exception classes for the Code Atelier Governance SDK.

Every public SDK exception carries three fields:
    user_message    -- safe to display to end users / HTTP callers
    detail          -- for server-side logs only (may contain internals)
    recovery_hint   -- actionable next step the developer can take
"""
from __future__ import annotations


class GovernanceError(Exception):
    """Base class for all SDK exceptions.

    Subclasses should pass ``recovery_hint`` so that developers seeing the
    error in logs or catch blocks know what to do next.
    """

    def __init__(
        self,
        message: str,
        *,
        recovery_hint: str = "",
    ) -> None:
        super().__init__(message)
        self.recovery_hint = recovery_hint
