"""Code Atelier Governance SDK — Enforcement gates for AI agents."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .sdk import GovernanceConfig, GovernanceSDK

try:
    __version__ = _pkg_version("codeatelier-governance")
except PackageNotFoundError:  # pragma: no cover
    # Fallback for source-tree usage where the package isn't installed
    __version__ = "0.0.0+source"

__all__ = ["GovernanceSDK", "GovernanceConfig", "__version__"]
