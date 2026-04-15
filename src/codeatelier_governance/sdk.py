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
from .audit.store import AuditStore, BatchingWriter, InMemoryAuditStore  # noqa: F401
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
    # When False, the audit substrate uses an in-memory ring buffer and no
    # persistence (no Postgres writes, no JSONL fallback).  ``sdk.audit``
    # still exists because every other module needs it to log events, but
    # those events are dropped on process exit.  Intended for test mode and
    # privacy-sensitive isolated runs.  Production deployments MUST leave
    # this True to preserve the HMAC-chained tamper-evident audit trail.
    enable_audit: bool = True
    # When False, ``sdk.gates``, ``sdk.scope``, ``sdk.cost`` do not exist.
    # Any code path that calls them raises AttributeError — loud rather
    # than silent.  Modules that depend on a disabled module are cascaded:
    # if ``enable_scope=False`` or ``enable_cost=False``, contracts is
    # automatically disabled because it has no way to enforce scope- or
    # budget-based pre-conditions.
    enable_gates: bool = True
    enable_scope: bool = True
    enable_cost: bool = True
    # Reserved for a future PromptsModule (stub lives at
    # codeatelier_governance.prompts).  Kept in the config for forward
    # compatibility so callers that pre-emptively set the flag do not
    # break when the real module lands.
    enable_prompts: bool = True
    # Loop detection: sliding-window repeated-tool-call detection + auto-halt.
    # Enabled by default.  Set to False to skip LoopModule construction;
    # sdk.loop will not exist and any call to it raises AttributeError.
    enable_loop: bool = True
    # Presence: agent heartbeat / live-idle-unresponsive tracking.
    # Enabled by default.  Set to False to skip PresenceModule construction;
    # sdk.presence will not exist and any call to it raises AttributeError.
    enable_presence: bool = True
    # Routing is an advisory feature that can mutate the model on an LLM
    # call.  It is OFF by default — enable it explicitly at SDK construction
    # time AND register at least one RoutingPolicy for it to take effect on
    # the request path.  Keeping this off-by-default matches the "every
    # module is opt-in via config, not code changes" invariant from
    # CLAUDE.md and prevents silent model substitution.
    enable_routing: bool = False
    # When True, any call to sdk.audit.get_events() verifies the HMAC chain
    # for the returned window before returning. Raises ChainIntegrityError if
    # any link fails. Default False: opt-in because verification cost is
    # O(n) in the number of events returned.
    verify_chain_on_read: bool = False
    # When True (the default), sdk.start() emits a structlog warning when no
    # LLM wrappers (wrap_anthropic / wrap_openai) have been registered.  Set
    # to False in test suites to suppress the warning.
    warn_on_no_wrappers: bool = True
    # Optional default max_tokens used by budget projection in wrappers when
    # the caller does not declare max_tokens explicitly.  Suppresses the
    # "max_tokens_not_declared" warning for projects that always use the same cap.
    # Must be >= 1 when provided; negative values would corrupt budget gate projection.
    default_max_tokens: int | None = None

    def __post_init__(self) -> None:
        """Validate field constraints that cannot be expressed as dataclass defaults."""
        if self.default_max_tokens is not None and self.default_max_tokens < 1:
            raise ValueError(
                f"GovernanceConfig.default_max_tokens must be >= 1 "
                f"(got {self.default_max_tokens}). "
                f"Negative or zero values would corrupt forward budget projection."
            )


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
        # Registry of wrapper labels recorded by wrap_anthropic() / wrap_openai().
        # Used at start() to warn operators when zero enforcement wrappers are active.
        self._registered_wrappers: list[str] = []

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

        # ------------------------------------------------------------------
        # Audit substrate (always constructed because every other module
        # needs it to log events).  When ``enable_audit=False`` the audit
        # module is backed by an in-memory ring buffer with no persistence,
        # no BatchingWriter, no JSONL fallback — events are accepted and
        # dropped.  ``sdk.audit`` still exists so dependent modules keep
        # working, but a loud warning is logged at init so operators know
        # audit persistence is off.
        # ------------------------------------------------------------------
        if self.config.enable_audit:
            store = self._build_audit_store(database_url, self._shared_engine)
            # Durable fallback: when the primary is down, audit events spill
            # to this on-disk JSONL so they survive process restarts.
            fallback = JsonlFallbackStore(
                fallback_path or DEFAULT_FALLBACK_PATH
            )
            writer = BatchingWriter(primary=store, fallback=fallback)
            self.audit = AuditModule(
                store,
                secret=resolved_secret,
                writer=writer,
                verify_chain_on_read=self.config.verify_chain_on_read,
            )
        else:
            logger.warning(
                "sdk.audit_disabled",
                detail=(
                    "enable_audit=False — audit events are accepted by the "
                    "API but NOT persisted.  No HMAC chain, no Postgres "
                    "write, no JSONL fallback.  Intended for test mode and "
                    "privacy-sensitive isolated runs only.  Set "
                    "enable_audit=True for any production deployment."
                ),
            )
            self.audit = AuditModule(
                InMemoryAuditStore(max_events=64),
                secret=resolved_secret,
            )

        # ------------------------------------------------------------------
        # Enforcement modules — each honors its own ``enable_X`` flag.  A
        # disabled module is NOT constructed; ``sdk.scope`` / ``sdk.cost`` /
        # ``sdk.gates`` simply do not exist when their flag is False, and
        # any code calling them gets a loud AttributeError instead of a
        # silent no-op.
        # ------------------------------------------------------------------
        if self.config.enable_scope:
            self.scope = ScopeModule(
                self.audit,
                database_url=database_url,
                engine=self._shared_engine,
            )
        else:
            logger.warning(
                "sdk.scope_disabled",
                detail=(
                    "enable_scope=False — sdk.scope is not constructed.  "
                    "Any call to sdk.scope.* will raise AttributeError."
                ),
            )

        if self.config.enable_cost:
            cost_store = self._build_cost_store(
                database_url, self._shared_engine
            )
            self.cost = CostModule(
                self.audit,
                store=cost_store,
                fail_open=cost_fail_open,
                database_url=database_url,
                engine=self._shared_engine,
            )
        else:
            logger.warning(
                "sdk.cost_disabled",
                detail=(
                    "enable_cost=False — sdk.cost is not constructed.  "
                    "Budget enforcement and cost tracking are off."
                ),
            )

        if self.config.enable_gates:
            gates_store = self._build_gates_store(
                database_url, self._shared_engine
            )
            self.gates = GatesModule(
                self.audit, secret=resolved_secret, store=gates_store,
            )
        else:
            logger.warning(
                "sdk.gates_disabled",
                detail=(
                    "enable_gates=False — sdk.gates is not constructed.  "
                    "HITL approval tokens cannot be minted or resolved."
                ),
            )

        if self.config.enable_loop:
            self.loop = LoopModule(
                self.audit,
                policies=loop_policies,
                database_url=database_url,
                engine=self._shared_engine,
            )
        else:
            logger.warning(
                "sdk.loop_disabled",
                detail=(
                    "enable_loop=False — sdk.loop is not constructed.  "
                    "Loop detection and auto-halt are off."
                ),
            )

        if self.config.enable_presence:
            self.presence = PresenceModule(
                database_url=database_url, engine=self._shared_engine,
            )
            # v0.5.4 kill switch wiring: scope.check() will fail-closed
            # with AgentKilledError when an operator clicks "Kill" in the
            # console. Setter is no-op if scope is also disabled.
            if hasattr(self, "scope") and hasattr(self.scope, "set_presence_module"):
                self.scope.set_presence_module(self.presence)
        else:
            logger.warning(
                "sdk.presence_disabled",
                detail=(
                    "enable_presence=False — sdk.presence is not constructed. "
                    "Agent heartbeat tracking AND the v0.5.4 kill switch are off. "
                    "Console kill clicks will record audit events but will NOT "
                    "halt agents at the SDK enforcement boundary."
                ),
            )

        # Contracts depends on scope + cost (+ gates for HITL).  Cascade
        # disable: if any hard dependency is off, contracts is also off and
        # sdk.contracts does not exist.  Log the cascade reason so operators
        # see exactly which flag triggered it.
        contracts_deps_ok = (
            self.config.enable_scope and self.config.enable_cost
        )
        if contracts_deps_ok:
            self.contracts = ContractsModule(
                self.audit,
                self.scope,
                self.cost,
                gates=self.gates if self.config.enable_gates else None,
            )
        else:
            missing = [
                name
                for name, enabled in (
                    ("scope", self.config.enable_scope),
                    ("cost", self.config.enable_cost),
                )
                if not enabled
            ]
            logger.warning(
                "sdk.contracts_disabled_cascade",
                detail=(
                    f"contracts module disabled because its dependencies "
                    f"are off: {', '.join(missing)}.  Re-enable these "
                    f"flags to use sdk.contracts.*."
                ),
            )

        # Routing is opt-in via ``enable_routing``.  When disabled, the
        # attribute does not exist on the SDK at all — wrappers use
        # ``getattr(sdk, "routing", None)`` to detect activation, and
        # ``sdk.routing.register(...)`` raises AttributeError to make
        # misconfiguration loud rather than silent.  Routing also requires
        # cost to be enabled (it calls cost.snapshot() to make budget-aware
        # decisions) — cascade disable if cost is off.
        if self.config.enable_routing:
            if not self.config.enable_cost:
                logger.warning(
                    "sdk.routing_disabled_cascade",
                    detail=(
                        "enable_routing=True but enable_cost=False.  "
                        "Routing requires the cost module for budget-aware "
                        "decisions.  sdk.routing is NOT constructed."
                    ),
                )
            else:
                from .routing.module import RoutingModule
                self.routing = RoutingModule(
                    self.audit,
                    self.cost,
                    scope=self.scope if self.config.enable_scope else None,
                    database_url=database_url,
                    engine=self._shared_engine,
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

        scope_count = 0
        cost_count = 0
        routing_count = 0

        if hasattr(self, "scope"):
            scope_policies = await self.scope.get_stored_policies()
            new_scope: dict[str, _ScopePolicy] = {
                p.agent_id: p for p in scope_policies
            }
            self.scope._policies = new_scope  # atomic dict replace under GIL
            scope_count = len(new_scope)

        if hasattr(self, "cost"):
            cost_policies = await self.cost.get_stored_policies()
            new_cost: dict[str, _BudgetPolicy] = {
                p.agent_id: p for p in cost_policies
            }
            self.cost._policies = new_cost
            cost_count = len(new_cost)

        if self.config.enable_routing and hasattr(self, "routing"):
            routing_policies = await self.routing.get_stored_policies()
            self.routing._policies = {
                p.agent_id: p for p in routing_policies
            }
            routing_count = len(self.routing._policies)

        self._last_policy_updated_at = max_updated

        logger.info(
            "hot_reload.policies_reloaded",
            scope_count=scope_count,
            cost_count=cost_count,
            routing_count=routing_count,
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

        Emits a structlog warning when no LLM wrappers have been registered
        (i.e. wrap_anthropic() or wrap_openai() was never called), unless
        ``warn_on_no_wrappers=False`` was passed at construction time.
        """
        self._started = True
        # self.audit always exists — either wired to a real persistent
        # substrate (enable_audit=True) or an in-memory ring buffer
        # (enable_audit=False).  Either way the writer lifecycle must run
        # so .log() calls from dependent modules handle their own buffer
        # state correctly.
        await self.audit.start()

        if self.config.warn_on_no_wrappers and not self._registered_wrappers:
            logger.warning(
                "no_wrappers_registered",
                message=(
                    "GovernanceSDK started with no wrappers registered. "
                    "Budget and scope enforcement are inactive. "
                    "Call wrap_anthropic() or wrap_openai() to activate enforcement."
                ),
            )
        # Drain policies that were registered synchronously (before any
        # event loop was running).  This replaces the v0.5.0 behaviour of
        # calling ``asyncio.run()`` inside ``register()``, which violated
        # invariant #3 and could deadlock sync startup.
        if hasattr(self, "scope"):
            await self.scope.flush_pending_upserts()
        if hasattr(self, "cost"):
            await self.cost.flush_pending_upserts()
        if hasattr(self, "routing"):
            await self.routing.flush_pending_upserts()
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
        await self.audit.close()
        if hasattr(self, "loop"):
            await self.loop.close()
        if hasattr(self, "presence"):
            await self.presence.close()
        # Dispose the shared engine last, after all modules have released
        # their references to it.
        if self._shared_engine is not None:
            await self._shared_engine.dispose()
            self._shared_engine = None

    # ------------------------------------------------------------------
    # Stable public API: audit chain verification
    # ------------------------------------------------------------------

    async def verify_chain(
        self,
        *,
        from_seq: int | None = None,
        to_seq: int | None = None,
        session_id: Any | None = None,
    ) -> bool:
        """Verify the HMAC audit chain for integrity.

        Delegates to :meth:`codeatelier_governance.audit.module.AuditModule.verify_chain`.

        Parameters
        ----------
        from_seq:
            0-based index of the first event to check (inclusive). ``None``
            starts from the beginning of the session's event list.
        to_seq:
            0-based index of the last event to check (inclusive). ``None``
            checks through the end.
        session_id:
            UUID of the session to verify. When ``None``, verifies all
            events known to the in-memory store (suited for tests; for
            production Postgres use, always pass an explicit session_id).

        Returns
        -------
        True
            Every checked link is intact.

        Raises
        ------
        codeatelier_governance.audit.errors.ChainIntegrityError
            Raised at the first failing link, carrying the 0-based sequence
            number of the tampered event.
        """
        return await self.audit.verify_chain(
            from_seq=from_seq,
            to_seq=to_seq,
            session_id=session_id,
        )

    async def __aenter__(self) -> "GovernanceSDK":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
