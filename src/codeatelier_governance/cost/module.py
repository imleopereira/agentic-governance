"""Spend / budget enforcement module exposed via ``sdk.cost``.

Public API:
    sdk.cost.register(policy)               # at SDK init
    await sdk.cost.track(agent_id, session_id, tokens=N, usd=X)
    await sdk.cost.check_or_raise(agent_id, session_id)
    snapshot = await sdk.cost.snapshot(agent_id, session_id)

Pattern:
    1. Pre-call: ``await sdk.cost.check_or_raise(agent_id, session_id)``
    2. Run the LLM / tool call
    3. Post-call: ``await sdk.cost.track(agent_id, session_id, tokens=..., usd=...)``

Every breach is auto-logged as an audit event with kind="budget.exceeded".

**Non-breaking guarantee:** ``track`` is an observation surface and NEVER
raises — internal failures (DB unreachable, validation, etc.) are logged
and swallowed so the host application call always continues.
``check_or_raise`` is an enforcement gate and DOES raise ``BudgetExceeded``
by contract; that's the whole point.

Multi-process correctness: when constructed with a ``PostgresCostStore``,
counters are atomic across all worker processes via row-level UPSERT
addition. Concurrent ``track`` calls from different workers all land with
no lost updates.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from ..audit.models import AuditEvent
from ..audit.module import AuditModule
from .errors import BudgetExceeded
from .models import BudgetPolicy, BudgetSnapshot
from .store import CostStore, InMemoryCostStore

logger = structlog.get_logger(__name__)


class CostModule:
    """Spend limits and budget tracking."""

    def __init__(
        self,
        audit: AuditModule,
        policies: list[BudgetPolicy] | None = None,
        store: CostStore | None = None,
        *,
        fail_open: bool = False,
        database_url: str | None = None,
        engine: Any = None,
        strict_unknown_models: bool = True,
        unknown_model_fallback_usd_per_million: float | None = None,
    ) -> None:
        """Construct the CostModule.

        Args:
            strict_unknown_models: When True (default, v0.6.2+), calls that
                reference a model not in ``MODEL_PRICING`` raise
                :class:`UnknownModelError`. This closes the silent-zero
                budget-bypass vector where a fine-tuned or custom model
                name (``my-ft-gpt4``) would never trip any USD cap. Set to
                False to opt into lax accounting (a structlog warning
                ``cost.unknown_model`` still fires).
            unknown_model_fallback_usd_per_million: In lax mode, the per-1M
                rate to apply to unknown models. ``None`` means 0.0 (the
                original foot-gun behavior — warning still emitted).
        """
        self._audit = audit
        self._store: CostStore = store or InMemoryCostStore()
        self._fail_open = fail_open
        self._strict_unknown_models = strict_unknown_models
        self._unknown_model_fallback_usd_per_million = (
            unknown_model_fallback_usd_per_million
        )
        self._policies: dict[str, BudgetPolicy] = {}
        self._no_policy_warned: set[str] = set()
        self._database_url = database_url
        self._engine: Any = engine
        self._owns_engine = False
        # Strong references to in-flight DB upsert tasks (GC safety).
        self._pending_upsert_tasks: set[asyncio.Task[Any]] = set()
        # Policies registered before any event loop was running.  Drained
        # by ``flush_pending_upserts()`` at SDK start().  Replaces the
        # v0.5.0 inline ``asyncio.run()`` anti-pattern.
        self._pending_upsert_policies: list[BudgetPolicy] = []
        # Optional reference to PresenceModule for the v0.5.4 halt switch.
        # Wired by GovernanceSDK after construction via set_presence_module().
        # If None, check_or_raise() runs without a halt check (back-compat with
        # v0.5.3 SDK construction). This is a deliberate optional dependency:
        # CostModule can still be constructed and tested in isolation.
        self._presence: Any = None
        for policy in policies or []:
            self._policies[policy.agent_id] = policy

    def set_presence_module(self, presence: Any) -> None:
        """Wire the PresenceModule for halt-switch enforcement (v0.6.2 P0).

        Called by GovernanceSDK during construction after both modules exist.
        Once set, every check_or_raise() call will first call
        presence.assert_not_halted(agent_id) and fail-closed with
        AgentHaltedError if the agent has been halted by an operator via
        the console. Closes the v0.5.4 bypass where only scope.check
        dispatched the halt check.
        """
        self._presence = presence

    def _get_engine(self) -> Any:
        """Return the shared engine, or lazily create one if no shared engine was provided."""
        if self._engine is not None:
            return self._engine
        if self._database_url is None:
            return None
        from sqlalchemy.ext.asyncio import create_async_engine

        url = self._database_url
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://"):]
        elif not url.startswith("postgresql+asyncpg://"):
            return None
        self._engine = create_async_engine(
            url, pool_pre_ping=True, pool_size=2, max_overflow=5,
        )
        self._owns_engine = True
        return self._engine

    def register(self, policy: BudgetPolicy) -> None:
        """Register a budget policy for an agent. Call at app startup."""
        self._policies[policy.agent_id] = policy
        self._persist_policy_best_effort(policy.agent_id, "budget", policy)

    def _persist_policy_best_effort(
        self, agent_id: str, policy_type: str, policy: BudgetPolicy,
    ) -> None:
        """Best-effort upsert of a policy to Postgres. Never raises.

        See :meth:`ScopeModule._persist_policy_best_effort` for the full
        design rationale — same defer-to-start pattern, replacing the
        v0.5.0 inline ``asyncio.run()`` which could deadlock sync
        startup in codebases that already owned an outer loop.
        """
        engine = self._get_engine()
        if engine is None:
            return
        try:
            loop: asyncio.AbstractEventLoop | None = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            if loop is not None and loop.is_running():
                task = loop.create_task(
                    self._upsert_policy(engine, agent_id, policy_type, policy)
                )
                self._pending_upsert_tasks.add(task)
                task.add_done_callback(
                    self._pending_upsert_tasks.discard
                )
            else:
                self._pending_upsert_policies.append(policy)
                logger.info(
                    "cost.persist_policy_deferred",
                    agent_id=agent_id,
                    policy_type=policy_type,
                    detail=(
                        "No event loop running at register() time; "
                        "upsert deferred until sdk.start()."
                    ),
                )
        except Exception as exc:
            logger.warning(
                "cost.persist_policy_failed",
                agent_id=agent_id,
                policy_type=policy_type,
                exc_type=type(exc).__name__,
            )

    async def flush_pending_upserts(self) -> None:
        """Drain policies registered before the event loop started."""
        if not self._pending_upsert_policies:
            return
        engine = self._get_engine()
        if engine is None:
            self._pending_upsert_policies.clear()
            return
        pending = self._pending_upsert_policies
        self._pending_upsert_policies = []
        for policy in pending:
            try:
                await self._upsert_policy(
                    engine, policy.agent_id, "budget", policy
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cost.deferred_upsert_failed",
                    agent_id=policy.agent_id,
                    exc_type=type(exc).__name__,
                )

    @staticmethod
    async def _upsert_policy(
        engine: Any,
        agent_id: str,
        policy_type: str,
        policy: BudgetPolicy,
    ) -> None:
        """Upsert a policy row into governance_policies."""
        from sqlalchemy import text

        policy_json = json.dumps(policy.model_dump(mode="json"))
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO governance_policies (agent_id, policy_type, policy_json, updated_at) "
                    "VALUES (:agent_id, :policy_type, CAST(:policy_json AS jsonb), NOW()) "
                    "ON CONFLICT (agent_id, policy_type) "
                    "DO UPDATE SET policy_json = CAST(:policy_json AS jsonb), updated_at = NOW()"
                ),
                {
                    "agent_id": agent_id,
                    "policy_type": policy_type,
                    "policy_json": policy_json,
                },
            )

    async def get_stored_policies(self) -> list[BudgetPolicy]:
        """Read budget policies from Postgres. Returns an empty list if no DB."""
        engine = self._get_engine()
        if engine is None:
            return []
        from sqlalchemy import text

        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT policy_json FROM governance_policies "
                    "WHERE policy_type = :policy_type "
                    "ORDER BY agent_id"
                ),
                {"policy_type": "budget"},
            )
            rows = list(res.mappings())
        policies: list[BudgetPolicy] = []
        for row in rows:
            data = row["policy_json"]
            if isinstance(data, str):
                data = json.loads(data)
            policies.append(BudgetPolicy.model_validate(data))
        return policies

    def get_policy(self, agent_id: str) -> BudgetPolicy | None:
        return self._policies.get(agent_id)

    async def close(self) -> None:
        """Release resources. Disposes the engine only if this module owns it."""
        await self._store.close()
        if self._owns_engine and self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def track_usage(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Track cost using built-in model pricing.

        Convenience wrapper around ``track()`` that looks up the model in
        the built-in pricing table. Users no longer need to pass ``usd=``
        manually for known models.

        Unknown-model handling respects the module-level
        ``strict_unknown_models`` flag (default True since v0.6.2):
            * strict=True: raises :class:`UnknownModelError` so the budget
              bypass is explicit. Tokens are NOT tracked in that case —
              the call has failed loudly.
            * strict=False: emits a ``cost.unknown_model`` warning and
              applies the configured fallback rate (or 0.0) to accumulate
              USD; tokens are still tracked normally.
        """
        from .errors import UnknownModelError
        from .pricing import estimate_cost

        try:
            usd = estimate_cost(
                model,
                input_tokens,
                output_tokens,
                strict=self._strict_unknown_models,
                fallback_usd_per_million=self._unknown_model_fallback_usd_per_million,
            )
        except UnknownModelError:
            # Raise so the host sees the misconfiguration; don't
            # silently accept tokens under an unknown model in strict mode.
            raise
        await self.track(
            agent_id,
            session_id,
            tokens=input_tokens + output_tokens,
            usd=usd,
            model=model,
        )

    async def track(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        tokens: int = 0,
        usd: float = 0.0,
        model: str | None = None,
    ) -> None:
        """Record post-call usage. NEVER raises.

        Counters are monotonic — only ever add. Negative deltas are
        rejected via a logged warning rather than an exception, because
        rejecting them would either silently lose the legitimate usage
        OR break the host application — neither is acceptable.
        """
        if tokens < 0 or usd < 0.0:
            logger.warning(
                "cost.track_negative_delta_rejected",
                agent_id=agent_id,
                tokens=tokens,
                usd=usd,
            )
            return
        try:
            await self._store.track(
                agent_id, session_id, tokens=tokens, usd=usd, model=model,
            )
        except Exception as exc:  # noqa: BLE001 - non-breaking guarantee
            logger.error(
                "cost.track_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
                session_id=str(session_id),
            )

    async def reconcile(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        projected_tokens: int,
        actual_tokens: int,
        projected_usd: float = 0.0,
        actual_usd: float = 0.0,
        model: str | None = None,
    ) -> None:
        """Reconcile projected usage with the provider's final reported usage.

        Used by streaming wrappers (v0.6.2 Bug #8): at stream start we
        optimistically ``track()`` ``projected_tokens`` (usually
        ``max_tokens``). When the stream completes, the provider reports
        the true ``actual_tokens`` count — which can be orders of magnitude
        larger than ``max_tokens`` when the provider stopped early, OR
        larger than ``max_tokens`` if the wrapper used a small SDK default.

        This method adds the DELTA (``actual - projected``) to the running
        counters so the totals match the provider-of-record. We never
        subtract — counters are monotonic — but the delta MAY exceed the
        max_tokens cap (hence the reconcile must happen AFTER the stream).

        If ``actual`` is less than ``projected``, we log a warning and
        DO NOT refund — monotonic counters prevent exploit patterns where
        an agent issues many cancelled streams to drain below its cap.
        """
        token_delta = actual_tokens - projected_tokens
        usd_delta = actual_usd - projected_usd
        if token_delta < 0 or usd_delta < 0.0:
            logger.warning(
                "cost.reconcile_negative_delta_ignored",
                agent_id=agent_id,
                session_id=str(session_id),
                projected_tokens=projected_tokens,
                actual_tokens=actual_tokens,
                projected_usd=projected_usd,
                actual_usd=actual_usd,
                detail=(
                    "Provider reported fewer tokens than projected. "
                    "Counters are monotonic — no refund is issued. "
                    "If this is routine, reduce max_tokens to avoid "
                    "over-reserving budget."
                ),
            )
            # Still track any positive dimension (e.g. tokens negative but usd positive).
            token_delta = max(0, token_delta)
            usd_delta = max(0.0, usd_delta)
        if token_delta == 0 and usd_delta == 0.0:
            return
        await self.track(
            agent_id,
            session_id,
            tokens=token_delta,
            usd=usd_delta,
            model=model,
        )

    async def model_breakdown(self, agent_id: str) -> dict[str, dict[str, float]]:
        """Return per-model usage for today.

        Returns ``{model: {"usd": X, "tokens": Y}}`` for the given agent.
        Defensive: if storage fails, returns empty dict and logs the error.
        """
        try:
            if hasattr(self._store, "get_model_breakdown"):
                result: dict[str, dict[str, float]] = await self._store.get_model_breakdown(agent_id)
                return result
            return {}
        except Exception as exc:
            logger.error(
                "cost.model_breakdown_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
            )
            return {}

    async def check_or_raise(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        projected_tokens: int | None = None,
    ) -> None:
        """Raise BudgetExceeded if any registered cap has been exceeded.

        This is an ENFORCEMENT gate: it raises by contract. The host call
        is expected to catch it and surface a 429 / quota error to the user.

        When ``projected_tokens`` is provided, the check is forward-looking:
        it evaluates ``current_balance + projected_tokens > limit`` instead
        of just ``current_balance > limit``. This ensures the LAST call
        before a hard limit is also blocked, eliminating the one-call-behind
        gap in the previous implementation.

        **Fail-closed semantics on storage failure.** If we cannot read the
        counter (DB unreachable, query timeout, etc.) we cannot verify the
        budget, and a fail-OPEN policy would let an attacker drain budgets
        by taking down the cost store. We FAIL CLOSED — deny the call,
        log critical, write a ``budget.check_failed`` audit row.

        Operators who explicitly want fail-open availability can construct
        ``CostModule(..., fail_open=True)`` and accept the risk.

        No-op for agents without a registered policy.

        Args:
            agent_id: The agent identifier to check.
            session_id: The current session UUID.
            projected_tokens: Optional token count for the upcoming call.
                When provided, the check projects current + projected against
                all token caps before allowing the call.
        """
        # v0.6.2 P0 halt switch — first thing in the gate.
        # If presence module is wired, fail-closed on halted agents BEFORE any
        # budget lookup so halted agents cannot keep burning budget. Skipped
        # silently if no presence module is configured (back-compat with v0.5.3
        # SDK construction). Closes the v0.5.4 bypass where only scope.check
        # dispatched the halt check.
        if self._presence is not None:
            await self._presence.assert_not_halted(agent_id)

        policy = self._policies.get(agent_id)
        if policy is None:
            if agent_id not in self._no_policy_warned:
                self._no_policy_warned.add(agent_id)
                logger.warning(
                    "cost.no_policy_registered",
                    agent_id=agent_id,
                    detail=(
                        f"No BudgetPolicy registered for agent {agent_id!r} "
                        f"-- budget enforcement is inactive. All calls will "
                        f"be allowed. Register a policy with "
                        f"sdk.cost.register(BudgetPolicy(agent_id=...))."
                    ),
                )
            return
        try:
            # Use the combined query when available (PostgresCostStore)
            # to halve the pre-call enforcement latency (1 DB round-trip
            # instead of 2).
            if hasattr(self._store, "get_session_and_daily_usage"):
                s_usd, s_tok, d_usd, d_tok = await self._store.get_session_and_daily_usage(
                    agent_id, session_id
                )
            else:
                s_usd, s_tok = await self._store.get_session_usage(
                    agent_id, session_id
                )
                d_usd, d_tok = await self._store.get_agent_daily_usage(agent_id)
        except Exception as exc:
            logger.critical(
                "cost.check_failed_fail_closed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
                fail_open=self._fail_open,
            )
            # Best-effort audit row — audit.log is itself non-breaking, so
            # this never raises even if the audit substrate is also down.
            await self._audit.log(
                AuditEvent(
                    agent_id=agent_id,
                    session_id=session_id,
                    kind="budget.check_failed",
                    metadata={
                        "reason": "cost store unreachable",
                        "error_type": type(exc).__name__,
                        "fail_mode": "open" if self._fail_open else "closed",
                    },
                )
            )
            if self._fail_open:
                logger.warning(
                    "cost.check_failed_allowing_call_per_fail_open",
                    agent_id=agent_id,
                )
                return
            raise BudgetExceeded(
                f"cost store unreachable; failing closed for safety. "
                f"agent_id={agent_id!r}, error={type(exc).__name__}. "
                f"Set fail_open=True at SDK init to allow the call instead "
                f"(NOT recommended for production).",
                recovery_hint="Check database connectivity. Set fail_open=True only for non-production.",
            )

        # When projected_tokens is provided, project current + projected against
        # token caps.  USD caps are not projected (we don't know the USD cost
        # until after the call) — they remain backward-looking as before.
        s_tok_projected = s_tok + projected_tokens if projected_tokens is not None else s_tok
        d_tok_projected = d_tok + projected_tokens if projected_tokens is not None else d_tok

        breach: tuple[str, float, float] | None = None
        if policy.per_session_usd is not None and s_usd > policy.per_session_usd:
            breach = ("per_session_usd", s_usd, policy.per_session_usd)
        elif (
            policy.per_session_tokens is not None
            and s_tok_projected > policy.per_session_tokens
        ):
            breach = (
                "per_session_tokens",
                float(s_tok_projected),
                float(policy.per_session_tokens),
            )
        elif (
            policy.per_agent_usd_daily is not None
            and d_usd > policy.per_agent_usd_daily
        ):
            breach = (
                "per_agent_usd_daily",
                d_usd,
                policy.per_agent_usd_daily,
            )
        elif (
            policy.per_agent_tokens_daily is not None
            and d_tok_projected > policy.per_agent_tokens_daily
        ):
            breach = (
                "per_agent_tokens_daily",
                float(d_tok_projected),
                float(policy.per_agent_tokens_daily),
            )

        if breach is not None:
            cap_name, used, limit = breach
            await self._audit.log(
                AuditEvent(
                    agent_id=agent_id,
                    session_id=session_id,
                    kind="budget.exceeded",
                    metadata={
                        "cap": cap_name,
                        "used": used,
                        "limit": limit,
                    },
                )
            )
            raise BudgetExceeded(
                f"Budget exceeded: {cap_name} for agent {agent_id!r} "
                f"({used:.4f} > {limit:.4f}).\n"
                f"Fix: increase the cap via BudgetPolicy({cap_name}=...) or start a new session.",
                recovery_hint=f"Increase {cap_name} in BudgetPolicy or create a new session.",
            )

        if policy.per_session_seconds is not None:
            elapsed: int | None = None
            try:
                # Prefer Postgres-side elapsed computation to avoid
                # mixed-clock skew between Python and database servers.
                if hasattr(self._store, "get_session_elapsed_seconds"):
                    elapsed_f = await self._store.get_session_elapsed_seconds(
                        agent_id, session_id
                    )
                    if elapsed_f is not None:
                        elapsed = int(elapsed_f)
                else:
                    started = await self._store.get_session_start_time(
                        agent_id, session_id
                    )
                    if started is not None:
                        elapsed = int(
                            (datetime.now(timezone.utc) - started).total_seconds()
                        )
            except Exception as exc:
                logger.error(
                    "cost.session_time_check_failed",
                    error_type=type(exc).__name__,
                    agent_id=agent_id,
                )
                elapsed = None
            if elapsed is not None:
                if elapsed > policy.per_session_seconds:
                    await self._audit.log(
                        AuditEvent(
                            agent_id=agent_id,
                            session_id=session_id,
                            kind="budget.exceeded",
                            metadata={
                                "cap": "per_session_seconds",
                                "used": float(elapsed),
                                "limit": float(policy.per_session_seconds),
                            },
                        )
                    )
                    raise BudgetExceeded(
                        f"Session time limit exceeded for agent {agent_id!r}: "
                        f"{elapsed}s > {policy.per_session_seconds}s.\n"
                        f"Fix: increase per_session_seconds in BudgetPolicy or start a new session.",
                        recovery_hint="Increase per_session_seconds or start a new session.",
                    )

    async def snapshot(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> BudgetSnapshot:
        """Read-only snapshot of current usage and remaining budget.

        Defensive: if storage fails, returns zeros and logs the error.
        """
        policy = self._policies.get(agent_id)
        try:
            s_usd, s_tok = await self._store.get_session_usage(
                agent_id, session_id
            )
            d_usd, d_tok = await self._store.get_agent_daily_usage(agent_id)
        except Exception as exc:
            logger.error(
                "cost.snapshot_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
            )
            s_usd, s_tok, d_usd, d_tok = 0.0, 0, 0.0, 0

        def _remaining_f(cap: float | None, used: float) -> float | None:
            return None if cap is None else max(0.0, cap - used)

        def _remaining_i(cap: int | None, used: int) -> int | None:
            return None if cap is None else max(0, cap - used)

        return BudgetSnapshot(
            agent_id=agent_id,
            session_usd_used=s_usd,
            session_tokens_used=s_tok,
            agent_daily_usd_used=d_usd,
            agent_daily_tokens_used=d_tok,
            session_usd_remaining=_remaining_f(
                policy.per_session_usd if policy else None, s_usd
            ),
            session_tokens_remaining=_remaining_i(
                policy.per_session_tokens if policy else None, s_tok
            ),
            agent_daily_usd_remaining=_remaining_f(
                policy.per_agent_usd_daily if policy else None, d_usd
            ),
            agent_daily_tokens_remaining=_remaining_i(
                policy.per_agent_tokens_daily if policy else None, d_tok
            ),
        )
