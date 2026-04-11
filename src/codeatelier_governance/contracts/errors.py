"""Contract enforcement exceptions."""
from __future__ import annotations


class ContractError(Exception):
    """Base class for contract module errors."""


class ContractViolation(ContractError):
    """Raised when a pre-condition fails or post-condition is not met."""
