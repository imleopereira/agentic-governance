"""Human-in-the-loop approval gates.

Public API:
    ApprovalRequest    — pending request handed back to callers
    GatesModule        — exposed via sdk.gates
    ApprovalPending    — non-blocking decorator's signal that a human is needed
    ApprovalDenied     — raised when an approval is explicitly denied
    ApprovalTimeout    — raised when wait_for() times out
    ApprovalTokenError — raised when a token is invalid (forged, expired, reused, mismatched)
"""
from .errors import (
    ApprovalDenied,
    ApprovalPending,
    ApprovalTimeout,
    ApprovalTokenError,
    GateError,
)
from .models import ApprovalRequest
from .module import GatesModule

__all__ = [
    "ApprovalDenied",
    "ApprovalPending",
    "ApprovalRequest",
    "ApprovalTimeout",
    "ApprovalTokenError",
    "GateError",
    "GatesModule",
]
