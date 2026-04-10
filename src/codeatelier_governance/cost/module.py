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

v0.1 limitations (documented, accepted):
    * In-memory state only. Restarts lose counters. v0.2 will add Postgres
      durability for cross-restart enforcement.
    * Single-process. Multi-worker apps undercount. v0.2 will add a
      shared-counter backend.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from ..audit.models import AuditEvent
from ..audit.module import AuditModule
from .errors import BudgetExceeded
from .models import BudgetPolicy, BudgetSnapshot


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class CostModule:
    """Spend limits and budget tracking."""

    def __init__(
        self,
        audit: AuditModule,
        policies: list[BudgetPolicy] | None = None,
    ) -> None:
        self._audit = audit
        self._policies: dict[str, BudgetPolicy] = {}
        self._session_usage: dict[tuple[str, UUID], tuple[float, int]] = {}
        self._agent_daily: dict[str, tuple[float, int, datetime]] = {}
        self._lock = asyncio.Lock()
        for policy in policies or []:
            self._policies[policy.agent_id] = policy

    def register(self, policy: BudgetPolicy) -> None:
        """Register a budget policy for an agent. Call at app startup."""
        self._policies[policy.agent_id] = policy

    def get_policy(self, agent_id: str) -> BudgetPolicy | None:
        return self._policies.get(agent_id)

    async def track(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        tokens: int = 0,
        usd: float = 0.0,
    ) -> None:
        """Record post-call usage. Counters are monotonic — only ever add."""
        if tokens < 0 or usd < 0.0:
            raise ValueError(
                f"cost.track: tokens and usd must be non-negative "
                f"(got tokens={tokens}, usd={usd}). "
                f"Fix: track only positive deltas."
            )
        now = datetime.now(timezone.utc)
        day = _utc_day_start(now)
        async with self._lock:
            cur_usd, cur_tok = self._session_usage.get(
                (agent_id, session_id), (0.0, 0)
            )
            self._session_usage[(agent_id, session_id)] = (
                cur_usd + usd,
                cur_tok + tokens,
            )
            d_usd, d_tok, d_day = self._agent_daily.get(
                agent_id, (0.0, 0, day)
            )
            if d_day < day:
                d_usd, d_tok, d_day = 0.0, 0, day
            self._agent_daily[agent_id] = (d_usd + usd, d_tok + tokens, d_day)

    async def check_or_raise(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> None:
        """Raise BudgetExceeded if any registered cap has been exceeded.

        No-op for agents without a registered policy. The intended pattern
        is to call this BEFORE the LLM/tool invocation; if it raises, the
        invocation is skipped.
        """
        policy = self._policies.get(agent_id)
        if policy is None:
            return  # default allow for agents without a policy
        async with self._lock:
            s_usd, s_tok = self._session_usage.get(
                (agent_id, session_id), (0.0, 0)
            )
            d_usd, d_tok, _ = self._agent_daily.get(agent_id, (0.0, 0, None))

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
                f"budget exceeded: {cap_name}={used} > limit={limit} "
                f"for agent_id={agent_id!r}"
            )

    async def snapshot(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> BudgetSnapshot:
        """Read-only snapshot of current usage and remaining budget."""
        policy = self._policies.get(agent_id)
        async with self._lock:
            s_usd, s_tok = self._session_usage.get(
                (agent_id, session_id), (0.0, 0)
            )
            d_usd, d_tok, _ = self._agent_daily.get(agent_id, (0.0, 0, None))

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
