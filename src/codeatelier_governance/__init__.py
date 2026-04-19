"""Code Atelier Governance SDK — Enforcement gates for AI agents."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .audit.models import AuditEvent
from .contracts.errors import ContractViolation
from .errors import GovernanceError
from .contracts.models import Contract, PostCondition, PreCondition
from .cost.models import BudgetPolicy
from .integrations.agt_wrap import AGTBridge, wrap_agt_agent
from .loop.errors import LoopDetected
from .loop.models import LoopPolicy
from .presence.models import AgentStatus
from .routing.models import RoutingPolicy
from .scope.models import ScopePolicy
from .sdk import GovernanceConfig, GovernanceSDK
from .sync import GovernanceSDKSync

try:
    __version__ = _pkg_version("code-atelier-governance")
except PackageNotFoundError:  # pragma: no cover
    # Fallback for source-tree usage where the package isn't installed
    __version__ = "0.0.0+source"

__all__ = [
    "AGTBridge",
    "AgentStatus",
    "AuditEvent",
    "BudgetPolicy",
    "Contract",
    "ContractViolation",
    "GovernanceConfig",
    "GovernanceError",
    "GovernanceSDK",
    "GovernanceSDKSync",
    "LoopDetected",
    "LoopPolicy",
    "PostCondition",
    "PreCondition",
    "RoutingPolicy",
    "ScopePolicy",
    "__version__",
    "wrap_agt_agent",
]
