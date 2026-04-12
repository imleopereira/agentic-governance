"""GovernanceSDK — single entry point for the Code Atelier Governance SDK.

Usage:
    from codeatelier_governance import GovernanceSDK
    from codeatelier_governance.audit import AuditEvent

    async def main():
        async with GovernanceSDK(database_url="postgresql://...") as sdk:
            await sdk.audit.log(AuditEvent(agent_id="my-agent", kind="tool.call"))
"""
from __future__ import annotations

import asyncio
import os
import secrets
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .audit.jsonl_store import JsonlFallbackStore
from .audit.module import AuditModule
from .audit.store import AuditStore, BatchingWriter, InMemoryAuditStore
from .contracts.module import ContractsModule
from .cost.module import CostModule
from .gates.module import GatesModule
from .loop.module import LoopModule
from .presence.module import PresenceModule
from .scope.module import ScopeModule
from .utils import normalize_db_url

logger = structlog.get_logger(__name__)

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

    v0.3 additions:
        sdk.loop     — sliding-window loop / anomaly detection (G4)
        sdk.presence — agent heartbeat / live-idle-unresponsive tracking

    Hot-reload:
        Pass ``hot_reload=True`` to automatically reload scope and budget
        policies from Postgres in the background, or call
        ``await sdk.start_hot_reload(interval_seconds=30)`` after init.
    """

    def __init__(
        self,
        database_url: str | None = None,
        api_key: str | None = None,
        audit_secret: bytes | None = None,
        *,
        fallback_path: str | Path | None = None,
        cost_fail_open: bool = False,
        loop_policies: list[Any] | None = None,
        hot_reload: bool = False,
        hot_reload_interval: int = 30,
        **kwargs: Any,
    ) -> None:
        if not database_url and not api_key:
            raise ValueError(
                "GovernanceSDK init failed: neither database_url nor api_key provided.\n"
                "Expected: a postgresql:// connection string OR a Code Atelier API key.\n"
                "Fix: GovernanceSDK(database_url=os.environ['GOVERNANCE_DATABASE_URL'])"
            )
        self.config = GovernanceConfig(
            database_url=database_url,
            api_key=api_key,
            audit_secret=audit_secret,
            **kwargs,
        )

        self._started = False
        self._hot_reload_enabled = hot_reload
        self._hot_reload_interval = hot_reload_interval
        self._hot_reload_task: asyncio.Task[None] | None = None
        self._last_policy_updated_at: datetime | None = None

        resolved_secret_str = audit_secret or self._resolve_audit_secret()
        resolved_secret = (
            resolved_secret_str.encode("utf-8")
            if isinstance(resolved_secret_str, str)
            else resolved_secret_str
        )

        # Create ONE shared AsyncEngine for all modules when using Postgres.
        # This drops max connections from ~74 to ~15 per SDK instance.
        self._shared_engine: Any = None
        if database_url is not None:
            from sqlalchemy.ext.asyncio import create_async_engine

            self._shared_engine = create_async_engine(
                normalize_db_url(database_url, component="sdk"),
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
                pool_timeout=3,
                connect_args={"command_timeout": 5},
            )

        store = self._build_audit_store(database_url, self._shared_engine)
        # Durable fallback: when the primary is down, audit events spill to
        # this on-disk JSONL so they survive process restarts and crashes.
        fallback = JsonlFallbackStore(fallback_path or DEFAULT_FALLBACK_PATH)
        writer = BatchingWriter(primary=store, fallback=fallback)
        self.audit = AuditModule(store, secret=resolved_secret, writer=writer)

        # All enforcement modules share the audit substrate; the gates module
        # reuses the same secret for HMAC-signed approval tokens.
        cost_store = self._build_cost_store(database_url, self._shared_engine)
        gates_store = self._build_gates_store(database_url, self._shared_engine)
        self.scope = ScopeModule(
            self.audit, database_url=database_url, engine=self._shared_engine,
        )
        self.cost = CostModule(
            self.audit,
            store=cost_store,
            fail_open=cost_fail_open,
            database_url=database_url,
            engine=self._shared_engine,
        )
        self.gates = GatesModule(
            self.audit, secret=resolved_secret, store=gates_store
        )
        # v0.3 modules
        self.loop = LoopModule(
            self.audit,
            policies=loop_policies,
            database_url=database_url,
            engine=self._shared_engine,
        )
        self.presence = PresenceModule(
            database_url=database_url, engine=self._shared_engine,
        )
        self.contracts = ContractsModule(
            self.audit, self.scope, self.cost, gates=self.gates,
        )

    # ------------------------------------------------------------------
    # Audit secret resolution
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Store builders
    # ------------------------------------------------------------------

    @staticmethod
    def _build_audit_store(
        database_url: str | None, engine: Any = None,
    ) -> AuditStore:
        if database_url is None:
            return InMemoryAuditStore()
        # Local import keeps sqlalchemy/asyncpg out of the import path when
        # callers run in pure in-memory mode (e.g. unit tests).
        from .audit.postgres_store import PostgresAuditStore

        if engine is not None:
            return PostgresAuditStore(engine=engine)
        return PostgresAuditStore(database_url)

    @staticmethod
    def _build_cost_store(
        database_url: str | None, engine: Any = None,
    ) -> Any:
        from .cost.store import InMemoryCostStore

        if database_url is None:
            return InMemoryCostStore()
        from .cost.postgres_store import PostgresCostStore

        if engine is not None:
            return PostgresCostStore(engine=engine)
        return PostgresCostStore(database_url)

    @staticmethod
    def _build_gates_store(
        database_url: str | None, engine: Any = None,
    ) -> Any:
        from .gates.store import InMemoryGatesStore

        if database_url is None:
            return InMemoryGatesStore()
        from .gates.postgres_store import PostgresGatesStore

        if engine is not None:
            return PostgresGatesStore(engine=engine)
        return PostgresGatesStore(database_url)

    # ------------------------------------------------------------------
    # Hot-reload
    # ------------------------------------------------------------------

    async def start_hot_reload(self, interval_seconds: int = 30) -> None:
        """Start background policy reload from Postgres.

        Polls ``governance_policies`` every ``interval_seconds`` seconds and
        atomically replaces the in-memory scope and budget policy dicts when
        ``updated_at`` changes. Safe to call multiple times — idempotent if
        a reload task is already running.

        Must be called from inside a running event loop (i.e. after
        ``await sdk.start()`` or inside an ``async with sdk`` block).

        Cancelled automatically on ``sdk.close()``.
        """
        if self._hot_reload_task is not None and not self._hot_reload_task.done():
            return  # Already running
        self._hot_reload_interval = interval_seconds
        loop = asyncio.get_running_loop()
        self._hot_reload_task = loop.create_task(self._hot_reload_loop())

    async def _hot_reload_loop(self) -> None:
        """Asyncio task: sleep → poll → repeat until cancelled."""
        while True:
            try:
                await asyncio.sleep(self._hot_reload_interval)
                await self._poll_policies()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hot_reload.poll_failed",
                    error_type=type(exc).__name__,
                )

    async def _poll_policies(self) -> None:
        """Read governance_policies and atomically replace in-memory dicts.

        Reuses the shared engine instead of creating/disposing a new engine
        on every 30-second poll cycle.
        """
        if self._shared_engine is None:
            return

        from sqlalchemy import text

        async with self._shared_engine.connect() as conn:
            res = await conn.execute(
                text("SELECT MAX(updated_at) FROM governance_policies")
            )
            row = res.first()
            max_updated: datetime | None = row[0] if row else None

        if max_updated is None:
            return
        if max_updated.tzinfo is None:
            max_updated = max_updated.replace(tzinfo=timezone.utc)

        if (
            self._last_policy_updated_at is not None
            and max_updated <= self._last_policy_updated_at
        ):
            return  # No changes since last poll

        # Policies changed — reload and atomically replace
        from .cost.models import BudgetPolicy as _BudgetPolicy
        from .scope.models import ScopePolicy as _ScopePolicy

        scope_policies = await self.scope.get_stored_policies()
        cost_policies = await self.cost.get_stored_policies()

        new_scope: dict[str, _ScopePolicy] = {p.agent_id: p for p in scope_policies}
        new_cost: dict[str, _BudgetPolicy] = {p.agent_id: p for p in cost_policies}

        # Dict assignment is atomic under the GIL
        self.scope._policies = new_scope
        self.cost._policies = new_cost
        self._last_policy_updated_at = max_updated

        logger.info(
            "hot_reload.policies_reloaded",
            scope_count=len(new_scope),
            cost_count=len(new_cost),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background tasks (audit batch flusher, hot-reload, etc.).

        When hot-reload is enabled, policies are loaded synchronously from
        Postgres BEFORE the background task starts. This eliminates the
        cold-start window where policies are empty (critical for serverless
        deployments like AWS Lambda).
        """
        self._started = True
        if self.config.enable_audit:
            await self.audit.start()
        if self._hot_reload_enabled:
            # Load policies immediately so the first request has them.
            # Without this, there is a 30-second gap where _policies is empty.
            try:
                await self._poll_policies()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hot_reload.initial_load_failed",
                    error_type=type(exc).__name__,
                    detail="Policies will load on first background poll cycle.",
                )
            await self.start_hot_reload(self._hot_reload_interval)

    async def close(self) -> None:
        """Drain in-flight events and release all resources."""
        if self._hot_reload_task is not None:
            self._hot_reload_task.cancel()
            try:
                await self._hot_reload_task
            except asyncio.CancelledError:
                pass
            self._hot_reload_task = None
        if self.config.enable_audit:
            await self.audit.close()
        await self.loop.close()
        await self.presence.close()
        # Dispose the shared engine last, after all modules have released
        # their references to it.
        if self._shared_engine is not None:
            await self._shared_engine.dispose()
            self._shared_engine = None

    async def __aenter__(self) -> "GovernanceSDK":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
