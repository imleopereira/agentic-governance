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


class TokenVersionTooOldError(ApprovalTokenError):
    """Raised when a legacy v1 token is parsed past its operator-configured
    sunset date (``GatesModule(accept_v1_until=...)``).

    Distinct from ``ApprovalTokenError`` (signature mismatch / expired /
    etc.) so operator UIs can render a specific "this deployment aged
    out v1 approval tokens on {date} — contact admin for a grace-window
    override" message instead of a generic invalid-token error.

    Subclass of ``ApprovalTokenError`` so existing ``except ApprovalTokenError``
    handlers continue to catch this case without code changes on upgrade.
    """
