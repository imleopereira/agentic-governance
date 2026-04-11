"""Cost / budget enforcement exceptions."""
from __future__ import annotations

from ..errors import GovernanceError


class CostError(GovernanceError):
    """Base class for cost module errors."""


class BudgetExceeded(CostError):
    """Raised when an agent or session exceeds a configured budget cap."""


class BudgetPolicyError(CostError):
    """Raised on invalid policy configuration."""
