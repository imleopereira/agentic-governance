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
    ) -> None:
        self._audit = audit
        self._store: CostStore = store or InMemoryCostStore()
        self._fail_open = fail_open
        self._policies: dict[str, BudgetPolicy] = {}
        self._database_url = database_url
        self._engine: Any = None
        for policy in policies or []:
            self._policies[policy.agent_id] = policy

    def _get_engine(self) -> Any:
        """Lazily create and return the SQLAlchemy async engine."""
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
        return self._engine

    def register(self, policy: BudgetPolicy) -> None:
        """Register a budget policy for an agent. Call at app startup."""
        self._policies[policy.agent_id] = policy
        self._persist_policy_best_effort(policy.agent_id, "budget", policy)

    def _persist_policy_best_effort(
        self, agent_id: str, policy_type: str, policy: BudgetPolicy,
    ) -> None:
        """Best-effort upsert of a policy to Postgres. Never raises."""
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
                loop.create_task(
                    self._upsert_policy(engine, agent_id, policy_type, policy)
                )
            else:
                asyncio.run(
                    self._upsert_policy(engine, agent_id, policy_type, policy)
                )
        except Exception as exc:
            logger.warning(
                "cost.persist_policy_failed",
                agent_id=agent_id,
                policy_type=policy_type,
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
                    "VALUES (:agent_id, :policy_type, :policy_json::jsonb, NOW()) "
                    "ON CONFLICT (agent_id, policy_type) "
                    "DO UPDATE SET policy_json = :policy_json::jsonb, updated_at = NOW()"
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
        await self._store.close()

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
        """
        from .pricing import estimate_cost

        usd = estimate_cost(model, input_tokens, output_tokens)
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
    ) -> None:
        """Raise BudgetExceeded if any registered cap has been exceeded.

        This is an ENFORCEMENT gate: it raises by contract. The host call
        is expected to catch it and surface a 429 / quota error to the user.

        **Fail-closed semantics on storage failure.** If we cannot read the
        counter (DB unreachable, query timeout, etc.) we cannot verify the
        budget, and a fail-OPEN policy would let an attacker drain budgets
        by taking down the cost store. We FAIL CLOSED — deny the call,
        log critical, write a ``budget.check_failed`` audit row.

        Operators who explicitly want fail-open availability can construct
        ``CostModule(..., fail_open=True)`` and accept the risk.

        No-op for agents without a registered policy.
        """
        policy = self._policies.get(agent_id)
        if policy is None:
            return
        try:
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

        breach: tuple[str, float, float] | None = None
        if policy.per_session_usd is not None and s_usd > policy.per_session_usd:
            breach = ("per_session_usd", s_usd, policy.per_session_usd)
        elif (
            policy.per_session_tokens is not None
            and s_tok > policy.per_session_tokens
        ):
            breach = (
                "per_session_tokens",
                float(s_tok),
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
            and d_tok > policy.per_agent_tokens_daily
        ):
            breach = (
                "per_agent_tokens_daily",
                float(d_tok),
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
            try:
                started = await self._store.get_session_start_time(
                    agent_id, session_id
                )
            except Exception as exc:
                logger.error(
                    "cost.session_time_check_failed",
                    error_type=type(exc).__name__,
                    agent_id=agent_id,
                )
                started = None
            if started is not None:
                elapsed = int(
                    (datetime.now(timezone.utc) - started).total_seconds()
                )
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
