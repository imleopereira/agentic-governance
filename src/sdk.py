"""Main SDK entry point."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class GovernanceConfig:
    """SDK configuration."""
    database_url: str | None = None
    api_key: str | None = None
    enable_audit: bool = True
    enable_gates: bool = True
    enable_scope: bool = True
    enable_cost: bool = True
    enable_prompts: bool = True

class GovernanceSDK:
    """Main entry point for the Governance SDK."""

    def __init__(self, database_url: str | None = None, api_key: str | None = None, **kwargs):
        if not database_url and not api_key:
            raise ValueError(
                "Provide either database_url (self-hosted) or api_key (managed). "
                "Example: GovernanceSDK(database_url='postgresql://...')"
            )
        self.config = GovernanceConfig(database_url=database_url, api_key=api_key, **kwargs)
