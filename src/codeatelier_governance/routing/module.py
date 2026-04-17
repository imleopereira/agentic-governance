"""Routing module exposed via ``sdk.routing``.

Public API:
    sdk.routing.register(policy)                  # at app startup
    model = await sdk.routing.suggest(agent_id, session_id, requested_model)
    policies = await sdk.routing.get_stored_policies()

The module is advisory only — ``suggest()`` NEVER raises. If anything goes
wrong, the requested model is returned unchanged and the error is logged.

Two strategies:
    ``cost_aware`` — routes to cheaper tiers when remaining budget is low.
    ``rules``      — explicit requested_model → target_model remapping.

Policy registration is persisted best-effort to ``governance_policies``
(``policy_type='routing'``) and emits a ``routing.policy_changed`` audit
event for the security trail.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import structlog

from ..audit.models import AuditEvent
from ..audit.module import AuditModule
from ..cost.module import CostModule
from .models import RoutingPolicy

logger = structlog.get_logger(__name__)


class RoutingModule:
    """Advisory model routing — suggests the optimal model, never blocks calls."""

    def __init__(
        self,
        audit: AuditModule,
        cost: CostModule,
        *,
        scope: Any = None,  # ScopeModule — optional to avoid circular import at type level
        database_url: str | None = None,
        engine: Any = None,
    ) -> None:
        self._audit = audit
        self._cost = cost
        self._scope = scope
        self._policies: dict[str, RoutingPolicy] = {}
        self._database_url = database_url
        self._engine: Any = engine
        self._owns_engine = False
        # Strong references to in-flight background tasks so Python's GC does
        # not collect them mid-execution. Tasks remove themselves via a done
        # callback when they complete.
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        # Policies registered before any event loop was running.  Drained
        # by ``flush_pending_upserts()`` at sdk.start().  Replaces the
        # v0.5.0 inline ``asyncio.run()`` anti-pattern.
        self._pending_upsert_policies: list[RoutingPolicy] = []
        if scope is None:
            logger.warning(
                "routing.scope_not_wired",
                detail=(
                    "No ScopeModule provided to RoutingModule. "
                    "allowed_models constraints in ScopePolicy will NOT be enforced. "
                    "Pass scope=sdk.scope when constructing RoutingModule."
                ),
            )

    def has_policies(self) -> bool:
        """Return True when at least one routing policy is registered.

        The wrapper integrations use this to decide whether to run the
        routing path at all.  When no policies are registered, wrappers
        skip the ``suggest()`` call entirely — there is literally zero
        overhead on the LLM call path for customers who have enabled
        routing at the SDK level but not yet registered any policy.
        """
        return bool(self._policies)

    def _track_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Hold a strong reference to a background task until it completes.

        Python's asyncio docs warn that tasks created with
        ``asyncio.create_task`` / ``loop.create_task`` may be garbage-collected
        mid-execution if no strong reference is held.  We keep them alive in
        ``self._pending_tasks`` and remove each on completion via a done
        callback.
        """
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Engine management (same pattern as CostModule / ScopeModule)
    # ------------------------------------------------------------------

    def _get_engine(self) -> Any:
        """Return the shared engine, or lazily create one if needed."""
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

    # ------------------------------------------------------------------
    # Policy registration
    # ------------------------------------------------------------------

    def register(self, policy: RoutingPolicy) -> None:
        """Register a routing policy for an agent.

        Call at app startup. Upserts to ``governance_policies`` with
        ``policy_type='routing'`` and emits a ``routing.policy_changed``
        audit event for the security trail.
        """
        self._policies[policy.agent_id] = policy
        self._persist_policy_best_effort(policy.agent_id, "routing", policy)

    def _persist_policy_best_effort(
        self, agent_id: str, policy_type: str, policy: RoutingPolicy,
    ) -> None:
        """Best-effort upsert of a policy to Postgres. Never raises."""
        engine = self._get_engine()

        # Always emit the audit event regardless of DB availability.
        audit_event = AuditEvent(
            agent_id=agent_id,
            kind="routing.policy_changed",
            metadata={
                "strategy": policy.strategy,
                "cheap_model": policy.cheap_model,
                "mid_model": policy.mid_model,
                "expensive_model": policy.expensive_model,
                "action": "registered",
            },
        )
        try:
            loop: asyncio.AbstractEventLoop | None = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            if loop is not None and loop.is_running():
                # Hold strong references via self._pending_tasks so GC does
                # not collect them mid-execution (Python asyncio best practice).
                self._track_task(loop.create_task(self._audit.log(audit_event)))
                if engine is not None:
                    self._track_task(
                        loop.create_task(
                            self._upsert_policy(
                                engine, agent_id, policy_type, policy
                            )
                        )
                    )
            else:
                # Sync-startup path: no event loop.  Defer BOTH the audit
                # event and the DB upsert to sdk.start() via
                # ``flush_pending_upserts()``.  Replaces v0.5.0 inline
                # ``asyncio.run()`` (invariant #3 violation).
                self._pending_upsert_policies.append(policy)
                logger.info(
                    "routing.persist_policy_deferred",
                    agent_id=agent_id,
                    policy_type=policy_type,
                    detail=(
                        "No event loop running at register() time; "
                        "upsert and policy_changed audit event deferred "
                        "until sdk.start()."
                    ),
                )
        except Exception as exc:
            logger.warning(
                "routing.persist_policy_failed",
                agent_id=agent_id,
                policy_type=policy_type,
                exc_type=type(exc).__name__,
            )

    @staticmethod
    async def _upsert_policy(
        engine: Any,
        agent_id: str,
        policy_type: str,
        policy: RoutingPolicy,
    ) -> None:
        """Upsert a policy row into governance_policies."""
        from sqlalchemy import text

        policy_json = json.dumps(policy.model_dump(mode="json"))
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO governance_policies "
                    "(agent_id, policy_type, policy_json, updated_at) "
                    "VALUES (:agent_id, :policy_type, CAST(:policy_json AS jsonb), NOW()) "
                    "ON CONFLICT (agent_id, policy_type) "
                    "DO UPDATE SET policy_json = CAST(:policy_json AS jsonb), "
                    "updated_at = NOW()"
                ),
                {
                    "agent_id": agent_id,
                    "policy_type": policy_type,
                    "policy_json": policy_json,
                },
            )

    async def flush_pending_upserts(self) -> None:
        """Drain policies registered before the event loop started.

        Emits the deferred ``routing.policy_changed`` audit event for each
        and best-effort upserts to the DB.  Never raises.
        """
        if not self._pending_upsert_policies:
            return
        engine = self._get_engine()
        pending = self._pending_upsert_policies
        self._pending_upsert_policies = []
        for policy in pending:
            audit_event = AuditEvent(
                agent_id=policy.agent_id,
                kind="routing.policy_changed",
                metadata={
                    "strategy": policy.strategy,
                    "cheap_model": policy.cheap_model,
                    "mid_model": policy.mid_model,
                    "expensive_model": policy.expensive_model,
                    "action": "registered",
                },
            )
            try:
                await self._audit.log(audit_event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "routing.deferred_audit_failed",
                    agent_id=policy.agent_id,
                    exc_type=type(exc).__name__,
                )
            if engine is not None:
                try:
                    await self._upsert_policy(
                        engine, policy.agent_id, "routing", policy
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "routing.deferred_upsert_failed",
                        agent_id=policy.agent_id,
                        exc_type=type(exc).__name__,
                    )

    # ------------------------------------------------------------------
    # Policy loading
    # ------------------------------------------------------------------

    async def get_stored_policies(self) -> list[RoutingPolicy]:
        """Load routing policies from Postgres.

        Returns an empty list when no DB is configured (in-memory mode).
        """
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
                {"policy_type": "routing"},
            )
            rows = list(res.mappings())
        policies: list[RoutingPolicy] = []
        for row in rows:
            data = row["policy_json"]
            if isinstance(data, str):
                data = json.loads(data)
            policies.append(RoutingPolicy.model_validate(data))
        return policies

    # ------------------------------------------------------------------
    # Core advisory logic
    # ------------------------------------------------------------------

    async def suggest(
        self,
        agent_id: str,
        session_id: UUID,
        requested_model: str,
        *,
        max_tokens: int = 1000,
    ) -> str:
        """Advisory model suggestion.

        Never raises — on any error returns ``requested_model`` unchanged
        and logs a ``routing.suggest_failed`` event.

        Strategy dispatch:
            ``cost_aware`` — compares remaining budget to configured thresholds
                             and routes down the tier chain when budget is low.
            ``rules``      — applies explicit model remapping from
                             ``policy.model_rules``.

        Args:
            agent_id: Identifies the agent whose policy to consult.
            session_id: Current session UUID (used for budget snapshots).
            requested_model: The model the caller initially requested.
            max_tokens: Expected output token count; used to estimate call cost.

        Returns:
            A model string — either ``requested_model`` or a cheaper
            alternative if policy dictates routing down.
        """
        try:
            return await self._suggest_impl(
                agent_id, session_id, requested_model, max_tokens=max_tokens
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "routing.suggest_failed",
                agent_id=agent_id,
                session_id=str(session_id),
                requested_model=requested_model,
                exc_type=type(exc).__name__,
            )
            # Best-effort audit event — never raises itself
            try:
                event = AuditEvent(
                    agent_id=agent_id,
                    session_id=session_id,
                    kind="routing.suggest_failed",
                    metadata={
                        "requested_model": requested_model,
                        "error_type": type(exc).__name__,
                    },
                )
                await self._audit.log(event)
            except Exception:  # noqa: BLE001
                pass
            return requested_model

    async def _suggest_impl(
        self,
        agent_id: str,
        session_id: UUID,
        requested_model: str,
        *,
        max_tokens: int,
    ) -> str:
        """Core suggestion logic — may raise; wrapped by suggest()."""
        policy = self._policies.get(agent_id)
        if policy is None:
            return requested_model

        if policy.strategy == "rules":
            return await self._apply_rules(
                agent_id, session_id, requested_model, policy
            )

        # cost_aware
        return await self._apply_cost_aware(
            agent_id, session_id, requested_model, policy, max_tokens=max_tokens
        )

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------

    async def _apply_rules(
        self,
        agent_id: str,
        session_id: UUID,
        requested_model: str,
        policy: RoutingPolicy,
    ) -> str:
        """Apply ``rules`` strategy."""
        target = policy.model_rules.get(requested_model)
        if target is None:
            # No rule matched — return unchanged, no event
            return requested_model

        # Enforce allowed_models from scope if configured
        selected = self._apply_allowed_models_constraint(
            agent_id, target, requested_model
        )
        if selected != requested_model:
            # Rule applied successfully (possibly modulo allowed_models).
            await self._emit_suggestion_event(
                agent_id=agent_id,
                session_id=session_id,
                requested_model=requested_model,
                selected_model=selected,
                reason="rules_match",
                strategy="rules",
                session_usd_remaining=None,
                daily_usd_remaining=None,
                estimated_cost_requested=0.0,
                estimated_cost_selected=0.0,
            )
        elif selected != target:
            # Rule matched BUT the target was blocked by allowed_models and we
            # fell back to requested_model.  Emit a visibility event so
            # operators can see the rule was suppressed — this would otherwise
            # be a silent no-op that looks like "no routing applied".
            await self._emit_suggestion_event(
                agent_id=agent_id,
                session_id=session_id,
                requested_model=requested_model,
                selected_model=selected,
                reason="allowed_models_constraint",
                strategy="rules",
                session_usd_remaining=None,
                daily_usd_remaining=None,
                estimated_cost_requested=0.0,
                estimated_cost_selected=0.0,
            )
        return selected

    async def _apply_cost_aware(
        self,
        agent_id: str,
        session_id: UUID,
        requested_model: str,
        policy: RoutingPolicy,
        *,
        max_tokens: int,
    ) -> str:
        """Apply ``cost_aware`` strategy."""
        from ..cost.pricing import estimate_cost

        snapshot = await self._cost.snapshot(agent_id, session_id)

        # Estimate call cost for AUDIT TRAIL METADATA ONLY.
        #
        # The routing DECISION is driven by absolute budget thresholds in the
        # policy (min_session_usd_for_expensive / min_daily_usd_for_expensive),
        # NOT by this estimate.  This heuristic exists solely so the
        # routing.suggestion audit event carries a readable relative-cost
        # comparison between the requested and selected models.
        #
        # Assumption: typical chat-style calls have far more output than
        # input, so we approximate input = max_tokens / 4 and output =
        # max_tokens.  Callers with large-context workloads (RAG, document
        # summarisation) should read the audit metadata as a lower bound, not
        # a precise cost projection.
        input_tokens = max_tokens // 4
        output_tokens = max_tokens
        # Routing cost estimation is audit-metadata only (see block comment
        # above). We deliberately pass ``strict=False`` so an unknown model
        # name in the requested_model field never crashes the advisory path —
        # the USD-bypass vector from Bug #7 lives in cost tracking, not here.
        estimated_cost_requested = estimate_cost(
            requested_model, input_tokens, output_tokens, strict=False,
        )

        reason: str | None = None
        should_route_down = False

        # Only apply expensive-model thresholds when the requested model IS
        # the expensive tier (or the only tier configured).
        is_expensive_tier = (
            policy.expensive_model is not None
            and requested_model == policy.expensive_model
        ) or (
            policy.expensive_model is None
            and policy.mid_model is not None
            and requested_model == policy.mid_model
        )

        if is_expensive_tier or policy.expensive_model is None:
            if (
                policy.min_session_usd_for_expensive is not None
                and snapshot.session_usd_remaining is not None
                and snapshot.session_usd_remaining
                < policy.min_session_usd_for_expensive
            ):
                should_route_down = True
                reason = "session_budget_low"

            if not should_route_down and (
                policy.min_daily_usd_for_expensive is not None
                and snapshot.agent_daily_usd_remaining is not None
                and snapshot.agent_daily_usd_remaining
                < policy.min_daily_usd_for_expensive
            ):
                should_route_down = True
                reason = "daily_budget_low"

        if not should_route_down:
            # Apply allowed_models constraint even if no route-down
            constrained = self._apply_allowed_models_constraint(
                agent_id, requested_model, requested_model
            )
            if constrained != requested_model:
                reason = "allowed_models_constraint"
                estimated_cost_selected = estimate_cost(
                    constrained, input_tokens, output_tokens, strict=False,
                )
                await self._emit_suggestion_event(
                    agent_id=agent_id,
                    session_id=session_id,
                    requested_model=requested_model,
                    selected_model=constrained,
                    reason=reason,
                    strategy="cost_aware",
                    session_usd_remaining=snapshot.session_usd_remaining,
                    daily_usd_remaining=snapshot.agent_daily_usd_remaining,
                    estimated_cost_requested=estimated_cost_requested,
                    estimated_cost_selected=estimated_cost_selected,
                )
                return constrained
            return requested_model

        # Route down: try mid_model first, then cheap_model, then fall back
        candidates: list[str] = []
        if policy.mid_model is not None and policy.mid_model != requested_model:
            candidates.append(policy.mid_model)
        if policy.cheap_model is not None and policy.cheap_model != requested_model:
            candidates.append(policy.cheap_model)

        selected = requested_model
        for candidate in candidates:
            constrained = self._apply_allowed_models_constraint(
                agent_id, candidate, requested_model
            )
            if constrained == candidate:
                selected = candidate
                break
            # candidate was blocked by allowed_models; try next tier

        if selected == requested_model:
            # All cheaper tiers are blocked or unavailable — check if
            # requested_model itself is allowed
            allowed = self._apply_allowed_models_constraint(
                agent_id, requested_model, requested_model
            )
            selected = allowed
            if allowed != requested_model:
                reason = "allowed_models_constraint"

        estimated_cost_selected = estimate_cost(
            selected, input_tokens, output_tokens, strict=False,
        )

        if selected != requested_model:
            await self._emit_suggestion_event(
                agent_id=agent_id,
                session_id=session_id,
                requested_model=requested_model,
                selected_model=selected,
                reason=reason or "session_budget_low",
                strategy="cost_aware",
                session_usd_remaining=snapshot.session_usd_remaining,
                daily_usd_remaining=snapshot.agent_daily_usd_remaining,
                estimated_cost_requested=estimated_cost_requested,
                estimated_cost_selected=estimated_cost_selected,
            )

        return selected

    def _apply_allowed_models_constraint(
        self,
        agent_id: str,
        proposed_model: str,
        fallback_model: str,
    ) -> str:
        """Return ``proposed_model`` if it passes ``allowed_models``, else ``fallback_model``.

        Reads ``allowed_models`` from the scope policy for ``agent_id`` if one
        is registered.  When the scope module is not available or the policy
        has no ``allowed_models`` restriction, the proposed model is returned
        unchanged.
        """
        if self._scope is None:
            return proposed_model
        scope_policy = self._scope.get_policy(agent_id)
        if scope_policy is None:
            return proposed_model
        allowed_models: frozenset[str] = getattr(
            scope_policy, "allowed_models", frozenset()
        )
        if not allowed_models:
            return proposed_model
        if proposed_model in allowed_models:
            return proposed_model
        # proposed_model is blocked; return fallback if it's allowed
        if fallback_model in allowed_models:
            return fallback_model
        # Both blocked — return fallback anyway (routing is advisory)
        return fallback_model

    async def _emit_suggestion_event(
        self,
        *,
        agent_id: str,
        session_id: UUID,
        requested_model: str,
        selected_model: str,
        reason: str,
        strategy: str,
        session_usd_remaining: float | None,
        daily_usd_remaining: float | None,
        estimated_cost_requested: float,
        estimated_cost_selected: float,
    ) -> None:
        """Emit a ``routing.suggestion`` audit event.

        Awaited directly by callers — no fire-and-forget task creation.
        Errors are swallowed so the advisory path never raises.
        """
        event = AuditEvent(
            agent_id=agent_id,
            session_id=session_id,
            kind="routing.suggestion",
            model=selected_model,
            metadata={
                "requested_model": requested_model,
                "selected_model": selected_model,
                "strategy": strategy,
                "reason": reason,
                "session_usd_remaining": session_usd_remaining,
                "daily_usd_remaining": daily_usd_remaining,
                "estimated_cost_requested": estimated_cost_requested,
                "estimated_cost_selected": estimated_cost_selected,
            },
        )
        try:
            await self._audit.log(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "routing.emit_event_failed",
                agent_id=agent_id,
                kind="routing.suggestion",
                exc_type=type(exc).__name__,
            )
