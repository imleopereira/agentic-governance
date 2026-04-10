"""GovernanceSDK — single entry point for the Code Atelier Governance SDK.

Usage:
    from codeatelier_governance import GovernanceSDK
    from codeatelier_governance.audit import AuditEvent

    async def main():
        async with GovernanceSDK(database_url="postgresql://...") as sdk:
            await sdk.audit.log(AuditEvent(agent_id="my-agent", kind="tool.call"))
"""
from __future__ import annotations

import os
import secrets
import warnings
from dataclasses import dataclass
from typing import Any

from pathlib import Path

from .audit.jsonl_store import JsonlFallbackStore
from .audit.module import AuditModule
from .audit.store import AuditStore, BatchingWriter, InMemoryAuditStore
from .cost.module import CostModule
from .gates.module import GatesModule
from .scope.module import ScopeModule

MIN_AUDIT_SECRET_BYTES = 32

DEFAULT_FALLBACK_PATH = (
    Path.home() / ".codeatelier_governance" / "audit_fallback.jsonl"
)


@dataclass
class GovernanceConfig:
    """Configuration for the GovernanceSDK.

    All fields have sensible defaults so the SDK can be initialized with a
    single ``database_url`` argument and start producing audit logs.
    """

    database_url: str | None = None
    api_key: str | None = None
    audit_secret: bytes | None = None
    enable_audit: bool = True
    enable_gates: bool = True
    enable_scope: bool = True
    enable_cost: bool = True
    enable_prompts: bool = True


class GovernanceSDK:
    """Main entry point for the Code Atelier Governance SDK.

    Initialization requires either ``database_url`` (self-hosted) or
    ``api_key`` (managed mode, v0.2). The audit secret is required for HMAC
    chain construction; if not passed explicitly, the SDK reads
    ``GOVERNANCE_AUDIT_SECRET`` from the environment, or generates an
    ephemeral secret with a warning (dev mode only).
    """

    def __init__(
        self,
        database_url: str | None = None,
        api_key: str | None = None,
        audit_secret: bytes | None = None,
        *,
        fallback_path: str | Path | None = None,
        cost_fail_open: bool = False,
        **kwargs: Any,
    ) -> None:
        if not database_url and not api_key:
            raise ValueError(
                "GovernanceSDK init failed: neither database_url nor api_key provided.\n"
                "Expected: a postgresql:// connection string OR a Code Atelier API key.\n"
                "Fix: GovernanceSDK(database_url='postgresql://user:pass@host/db')"
            )
        self.config = GovernanceConfig(
            database_url=database_url,
            api_key=api_key,
            audit_secret=audit_secret,
            **kwargs,
        )

        resolved_secret = audit_secret or self._resolve_audit_secret()

        store = self._build_audit_store(database_url)
        # Durable fallback: when the primary is down, audit events spill to
        # this on-disk JSONL so they survive process restarts and crashes.
        fallback = JsonlFallbackStore(
            fallback_path or DEFAULT_FALLBACK_PATH
        )
        writer = BatchingWriter(primary=store, fallback=fallback)
        self.audit = AuditModule(store, secret=resolved_secret, writer=writer)
        # Three enforcement modules share the audit substrate; the gates
        # module reuses the same secret for HMAC-signed approval tokens.
        # When a database_url is provided, all enforcement modules use
        # Postgres-backed stores for multi-process correctness; otherwise
        # in-memory single-process stores.
        cost_store = self._build_cost_store(database_url)
        gates_store = self._build_gates_store(database_url)
        self.scope = ScopeModule(self.audit, database_url=database_url)
        self.cost = CostModule(
            self.audit,
            store=cost_store,
            fail_open=cost_fail_open,
            database_url=database_url,
        )
        self.gates = GatesModule(
            self.audit, secret=resolved_secret, store=gates_store
        )

    @staticmethod
    def _resolve_audit_secret() -> bytes:
        env_value = os.environ.get("GOVERNANCE_AUDIT_SECRET")
        if env_value is not None:
            data = env_value.encode("utf-8")
            if len(data) < MIN_AUDIT_SECRET_BYTES:
                raise ValueError(
                    f"GOVERNANCE_AUDIT_SECRET must be at least "
                    f"{MIN_AUDIT_SECRET_BYTES} bytes (got {len(data)}).\n"
                    f"Fix: export GOVERNANCE_AUDIT_SECRET=$(python -c "
                    f"'import secrets; print(secrets.token_hex(32))')"
                )
            return data
        warnings.warn(
            "No GOVERNANCE_AUDIT_SECRET set; generating an ephemeral secret. "
            "Audit chain verification will not survive restarts. "
            "Set GOVERNANCE_AUDIT_SECRET in production.",
            stacklevel=3,
        )
        return secrets.token_bytes(MIN_AUDIT_SECRET_BYTES)

    @staticmethod
    def _build_audit_store(database_url: str | None) -> AuditStore:
        if database_url is None:
            return InMemoryAuditStore()
        # Local import keeps sqlalchemy/asyncpg out of the import path when
        # callers run in pure in-memory mode (e.g. unit tests).
        from .audit.postgres_store import PostgresAuditStore

        return PostgresAuditStore(database_url)

    @staticmethod
    def _build_cost_store(database_url: str | None):  # type: ignore[no-untyped-def]
        from .cost.store import InMemoryCostStore

        if database_url is None:
            return InMemoryCostStore()
        from .cost.postgres_store import PostgresCostStore

        return PostgresCostStore(database_url)

    @staticmethod
    def _build_gates_store(database_url: str | None):  # type: ignore[no-untyped-def]
        from .gates.store import InMemoryGatesStore

        if database_url is None:
            return InMemoryGatesStore()
        from .gates.postgres_store import PostgresGatesStore

        return PostgresGatesStore(database_url)

    async def start(self) -> None:
        """Start background tasks (audit batch flusher, etc)."""
        if self.config.enable_audit:
            await self.audit.start()

    async def close(self) -> None:
        """Drain in-flight events and release resources."""
        if self.config.enable_audit:
            await self.audit.close()

    async def __aenter__(self) -> "GovernanceSDK":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
