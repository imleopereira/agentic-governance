"""Agent behavioral contracts — pre/post condition enforcement.

Public API:
    Contract           — declarative contract binding pre/post conditions to a tool
    PreCondition       — a pre-condition check definition
    PostCondition      — a post-condition check definition
    ContractViolation  — raised when a condition fails
    ContractsModule    — exposed via sdk.contracts
"""
from .errors import ContractError, ContractViolation
from .models import Contract, PostCondition, PreCondition
from .module import ContractsModule

__all__ = [
    "Contract",
    "ContractError",
    "ContractViolation",
    "ContractsModule",
    "PostCondition",
    "PreCondition",
]
