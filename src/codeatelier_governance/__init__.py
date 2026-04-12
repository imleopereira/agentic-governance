"""Code Atelier Governance SDK — Enforcement gates for AI agents."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .audit.models import AuditEvent
from .contracts.errors import ContractViolation
from .contracts.models import Contract, PostCondition, PreCondition
from .cost.models import BudgetPolicy
from .loop.errors import LoopDetected
from .loop.models import LoopPolicy
from .presence.models import AgentStatus
from .scope.models import ScopePolicy
from .sdk import GovernanceConfig, GovernanceSDK
from .sync import GovernanceSDKSync

try:
    __version__ = _pkg_version("code-atelier-governance")
except PackageNotFoundError:  # pragma: no cover
    # Fallback for source-tree usage where the package isn't installed
    __version__ = "0.0.0+source"

__all__ = [
    "AgentStatus",
    "AuditEvent",
    "BudgetPolicy",
    "Contract",
    "ContractViolation",
    "GovernanceConfig",
    "GovernanceSDK",
    "GovernanceSDKSync",
    "LoopDetected",
    "LoopPolicy",
    "PostCondition",
    "PreCondition",
    "ScopePolicy",
    "__version__",
]
