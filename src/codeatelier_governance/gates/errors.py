"""Human-in-the-loop gate exceptions."""
from __future__ import annotations

from ..errors import GovernanceError


class GateError(GovernanceError):
    """Base class for HITL gate errors."""


class ApprovalPending(GateError):
    """Raised by the non-blocking decorator path when approval is required.

    Carries the request_id so the caller can show it to a human and resume
    later by retrying with an approval token.
    """

    def __init__(self, request_id: str) -> None:
        super().__init__(
            f"approval required (request_id={request_id}). "
            f"Resolve via sdk.gates.grant(token) or sdk.gates.deny(token).",
            recovery_hint="Call sdk.gates.grant(token) or sdk.gates.deny(token) to resolve.",
        )
        self.request_id = request_id


class ApprovalDenied(GateError):
    """Raised when an approval request was explicitly denied by a human."""


class ApprovalTimeout(GateError):
    """Raised when wait_for() exceeds its timeout without resolution."""


class ApprovalTokenError(GateError):
    """Raised when an approval token is invalid: bad signature, expired, reused, or mismatched action_hash."""
