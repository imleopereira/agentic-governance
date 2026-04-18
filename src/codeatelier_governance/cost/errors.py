"""Cost / budget enforcement exceptions."""
from __future__ import annotations

from ..errors import GovernanceError


class CostError(GovernanceError):
    """Base class for cost module errors."""


class BudgetExceeded(CostError):
    """Raised when an agent or session exceeds a configured budget cap."""


class BudgetPolicyError(CostError):
    """Raised on invalid policy configuration."""


class UnknownModelError(CostError):
    """Raised by ``estimate_cost`` when the model is not in ``MODEL_PRICING``.

    Introduced in v0.6.2 to close the silent-zero budget-bypass vector:
    before this change, ``estimate_cost('my-ft-gpt4', ...)`` returned 0.0,
    meaning any USD cap was silently never tripped for custom model names.

    Opt out via ``estimate_cost(..., strict=False, fallback_usd_per_million=X)``
    if the host application wants lax accounting; a structlog warning
    ``cost.unknown_model`` fires each time to preserve visibility.
    """
