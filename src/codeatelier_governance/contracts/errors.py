"""Contract enforcement exceptions."""
from __future__ import annotations

from ..errors import GovernanceError


class ContractError(GovernanceError):
    """Base class for contract module errors."""


class ContractViolation(ContractError):
    """Raised when a pre-condition fails or post-condition is not met."""
