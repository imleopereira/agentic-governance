"""Spend limits and budget gates.

Public API:
    BudgetPolicy     — declarative caps per session and per agent/day
    BudgetSnapshot   — read-only usage snapshot
    CostModule       — exposed via sdk.cost
    BudgetExceeded   — raised when a cap is breached
"""
from .errors import BudgetExceeded, BudgetPolicyError, CostError
from .models import BudgetPolicy, BudgetSnapshot
from .module import CostModule

__all__ = [
    "BudgetExceeded",
    "BudgetPolicy",
    "BudgetPolicyError",
    "BudgetSnapshot",
    "CostError",
    "CostModule",
]
