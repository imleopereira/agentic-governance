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
    ) -> None:
        self._audit = audit
        self._store: CostStore = store or InMemoryCostStore()
        self._policies: dict[str, BudgetPolicy] = {}
        for policy in policies or []:
            self._policies[policy.agent_id] = policy

    def register(self, policy: BudgetPolicy) -> None:
        """Register a budget policy for an agent. Call at app startup."""
        self._policies[policy.agent_id] = policy

    def get_policy(self, agent_id: str) -> BudgetPolicy | None:
        return self._policies.get(agent_id)

    async def close(self) -> None:
        await self._store.close()

    async def track(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        tokens: int = 0,
        usd: float = 0.0,
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
                agent_id, session_id, tokens=tokens, usd=usd
            )
        except Exception as exc:  # noqa: BLE001 - non-breaking guarantee
            logger.error(
                "cost.track_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
                session_id=str(session_id),
            )

    async def check_or_raise(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> None:
        """Raise BudgetExceeded if any registered cap has been exceeded.

        This is an ENFORCEMENT gate: it raises by contract. The host call
        is expected to catch it and surface a 429 / quota error to the user.

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
            # Storage failure on a check is a special case: we cannot
            # determine whether the cap is exceeded. Default policy is to
            # FAIL OPEN (allow the call) rather than block on uncertainty,
            # because blocking on every storage hiccup would be worse than
            # over-allowing during an outage. Logged so operators can react.
            logger.error(
                "cost.check_failed_fail_open",
                error_type=type(exc).__name__,
                agent_id=agent_id,
            )
            return

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
