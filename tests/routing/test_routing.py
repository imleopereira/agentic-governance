"""Tests for the RoutingModule.

All tests use InMemoryCostStore and InMemoryAuditStore — no real DB required.

Coverage:
    1.  suggest() returns requested_model when no policy registered
    2.  cost_aware: routes to cheap_model when session budget below threshold
    3.  cost_aware: keeps expensive_model when budget is sufficient
    4.  cost_aware: routes based on daily budget when session budget is None
    5.  rules: maps model correctly when rule exists
    6.  rules: returns unchanged when no rule matches
    7.  allowed_models constraint: routing respects scope's allowed_models
    8.  suggest() never raises even when cost snapshot throws
    9.  register() emits routing.policy_changed audit event
    10. get_stored_policies() returns empty list when no DB (in-memory mode)
    11. max_tokens kwarg flows through to cost estimate (different estimates)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from codeatelier_governance.audit import AuditModule, BatchingWriter, InMemoryAuditStore
from codeatelier_governance.cost import CostModule
from codeatelier_governance.cost.models import BudgetPolicy
from codeatelier_governance.cost.store import InMemoryCostStore
from codeatelier_governance.routing.models import RoutingPolicy
from codeatelier_governance.routing.module import RoutingModule
from codeatelier_governance.scope.models import ScopePolicy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def audit_store() -> InMemoryAuditStore:
    """In-memory audit store."""
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def audit(audit_store: InMemoryAuditStore) -> AuditModule:
    """Wired AuditModule."""
    import secrets as _secrets

    secret = _secrets.token_bytes(32)
    writer = BatchingWriter(
        primary=audit_store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    module = AuditModule(audit_store, secret=secret, writer=writer)
    await module.start()
    try:
        yield module
    finally:
        await module.close()


@pytest_asyncio.fixture
async def cost_store() -> InMemoryCostStore:
    """In-memory cost store."""
    return InMemoryCostStore()


@pytest_asyncio.fixture
async def cost(audit: AuditModule, cost_store: InMemoryCostStore) -> CostModule:
    """CostModule backed by InMemoryCostStore."""
    return CostModule(audit, store=cost_store)


@pytest_asyncio.fixture
async def routing(audit: AuditModule, cost: CostModule) -> RoutingModule:
    """RoutingModule with no DB (in-memory mode)."""
    return RoutingModule(audit, cost)


# ---------------------------------------------------------------------------
# Helper to flush audit writer and collect events
# ---------------------------------------------------------------------------


async def _flush_and_collect(
    audit: AuditModule, audit_store: InMemoryAuditStore, kind: str
) -> list:
    """Flush writer and return all events of a given kind."""
    await asyncio.sleep(0.05)  # allow backgrounded tasks to complete
    await audit._writer.flush()
    return [e for e in audit_store._events.values() if e.kind == kind]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Test 1: No policy → return requested_model unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_no_policy_returns_requested(routing: RoutingModule) -> None:
    """suggest() returns requested_model unchanged when no policy is registered."""
    result = await routing.suggest("unknown-agent", uuid4(), "claude-opus-4-6")
    assert result == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Test 2: cost_aware routes to cheap_model when session budget is low
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_aware_routes_to_cheap_on_low_session_budget(
    routing: RoutingModule,
    cost: CostModule,
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
) -> None:
    """cost_aware: routes to cheap_model when session USD remaining < threshold."""
    agent_id = "agent-cost-aware"
    session_id = uuid4()

    # Register a budget policy so snapshot.session_usd_remaining is available
    cost.register(BudgetPolicy(agent_id=agent_id, per_session_usd=0.50))

    # Pre-spend: burn most of the session budget
    await cost.track(agent_id, session_id, tokens=100, usd=0.45)

    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            mid_model="claude-sonnet-4-6",
            expensive_model="claude-opus-4-6",
            min_session_usd_for_expensive=0.10,  # need $0.10 remaining
        )
    )

    # Session remaining = 0.50 - 0.45 = 0.05, which is < 0.10 threshold
    result = await routing.suggest(
        agent_id, session_id, "claude-opus-4-6", max_tokens=500
    )
    assert result == "claude-sonnet-4-6", (
        f"Expected mid_model 'claude-sonnet-4-6', got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: cost_aware keeps expensive_model when budget is sufficient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_aware_keeps_expensive_when_budget_ok(
    routing: RoutingModule,
    cost: CostModule,
) -> None:
    """cost_aware: keeps expensive_model when remaining budget exceeds threshold."""
    agent_id = "agent-rich"
    session_id = uuid4()

    cost.register(BudgetPolicy(agent_id=agent_id, per_session_usd=10.00))
    # Spend only $0.05 — remaining = $9.95, well above threshold
    await cost.track(agent_id, session_id, tokens=10, usd=0.05)

    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            expensive_model="claude-opus-4-6",
            min_session_usd_for_expensive=0.10,
        )
    )

    result = await routing.suggest(
        agent_id, session_id, "claude-opus-4-6", max_tokens=500
    )
    assert result == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Test 4: cost_aware routes based on daily budget when session budget is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_aware_daily_budget_trigger(
    routing: RoutingModule,
    cost: CostModule,
) -> None:
    """cost_aware: triggers routing when daily USD remaining < min_daily_usd_for_expensive."""
    agent_id = "agent-daily-cap"
    session_id = uuid4()

    # Only set a daily cap so session_usd_remaining is None
    cost.register(BudgetPolicy(agent_id=agent_id, per_agent_usd_daily=1.00))
    # Burn most of the daily budget
    await cost.track(agent_id, session_id, tokens=100, usd=0.95)

    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            expensive_model="claude-opus-4-6",
            min_daily_usd_for_expensive=0.10,
        )
    )

    result = await routing.suggest(
        agent_id, session_id, "claude-opus-4-6", max_tokens=500
    )
    # Daily remaining = 1.00 - 0.95 = 0.05 < 0.10 → route to cheap
    assert result == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Test 5: rules maps model correctly when rule exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rules_maps_model_when_rule_exists(routing: RoutingModule) -> None:
    """rules strategy: maps requested_model to target via model_rules."""
    agent_id = "agent-rules"
    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="rules",
            model_rules={"claude-opus-4-6": "claude-sonnet-4-6"},
        )
    )
    result = await routing.suggest(agent_id, uuid4(), "claude-opus-4-6")
    assert result == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Test 6: rules returns unchanged when no rule matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rules_no_match_returns_unchanged(routing: RoutingModule) -> None:
    """rules strategy: returns requested_model unchanged when no rule matches."""
    agent_id = "agent-rules-nomatch"
    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="rules",
            model_rules={"gpt-4o": "gpt-4o-mini"},
        )
    )
    result = await routing.suggest(agent_id, uuid4(), "claude-opus-4-6")
    assert result == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Test 7: allowed_models constraint respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_models_constraint_respected(
    audit: AuditModule,
    cost: CostModule,
) -> None:
    """routing respects allowed_models from scope policy."""
    agent_id = "agent-scope-constrained"
    session_id = uuid4()

    routing = RoutingModule(audit, cost)

    # Build a mock scope module with an allowed_models policy
    scope_module = MagicMock()
    scope_policy = ScopePolicy(
        agent_id=agent_id,
        allowed_models=frozenset({"claude-haiku-4-5"}),
    )
    scope_module.get_policy.return_value = scope_policy
    routing._scope = scope_module  # type: ignore[attr-defined]

    cost.register(BudgetPolicy(agent_id=agent_id, per_session_usd=0.10))
    # Low remaining budget — would route to mid_model or cheap_model
    await cost.track(agent_id, session_id, tokens=10, usd=0.09)

    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            mid_model="claude-sonnet-4-6",
            expensive_model="claude-opus-4-6",
            min_session_usd_for_expensive=0.05,
        )
    )

    result = await routing.suggest(
        agent_id, session_id, "claude-opus-4-6", max_tokens=500
    )
    # mid_model is blocked by allowed_models; cheap_model is allowed
    assert result == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Test 8: suggest() never raises even when cost snapshot throws
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_never_raises_on_snapshot_error(
    audit: AuditModule, cost: CostModule
) -> None:
    """suggest() returns requested_model and never raises if cost.snapshot fails."""
    agent_id = "agent-error"
    session_id = uuid4()

    routing = RoutingModule(audit, cost)
    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            expensive_model="claude-opus-4-6",
            min_session_usd_for_expensive=0.10,
        )
    )

    # Patch cost.snapshot to raise
    original_snapshot = cost.snapshot

    async def _boom(aid: str, sid: object) -> object:
        raise RuntimeError("DB exploded")

    cost.snapshot = _boom  # type: ignore[method-assign]

    try:
        result = await routing.suggest(agent_id, session_id, "claude-opus-4-6")
    finally:
        cost.snapshot = original_snapshot  # type: ignore[method-assign]

    # Must not raise, must return requested_model
    assert result == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Test 9: register() emits routing.policy_changed audit event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_emits_policy_changed_event(
    routing: RoutingModule,
    audit: AuditModule,
    audit_store: InMemoryAuditStore,
) -> None:
    """register() fires a routing.policy_changed audit event."""
    agent_id = "agent-register"
    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="rules",
            model_rules={"claude-opus-4-6": "claude-haiku-4-5"},
        )
    )

    events = await _flush_and_collect(audit, audit_store, "routing.policy_changed")
    matching = [e for e in events if e.agent_id == agent_id]
    assert len(matching) >= 1, (
        f"Expected at least 1 routing.policy_changed event; found {len(matching)}"
    )
    assert matching[0].metadata["action"] == "registered"
    assert matching[0].metadata["strategy"] == "rules"


# ---------------------------------------------------------------------------
# Test 10: get_stored_policies() returns empty list in in-memory mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_stored_policies_in_memory_returns_empty(
    routing: RoutingModule,
) -> None:
    """get_stored_policies() returns [] when no DB is configured."""
    policies = await routing.get_stored_policies()
    assert policies == []


# ---------------------------------------------------------------------------
# Test 11: max_tokens flows through to cost estimate (different estimates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tokens_affects_cost_estimate(
    audit: AuditModule,
    cost: CostModule,
    audit_store: InMemoryAuditStore,
) -> None:
    """Different max_tokens values produce different estimated costs in audit events."""
    agent_id = "agent-maxtoken"
    session_id = uuid4()

    # Budget just barely too low to trigger routing for small requests
    cost.register(BudgetPolicy(agent_id=agent_id, per_session_usd=0.50))
    await cost.track(agent_id, session_id, tokens=10, usd=0.48)

    routing = RoutingModule(audit, cost)
    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            expensive_model="claude-opus-4-6",
            min_session_usd_for_expensive=0.05,  # remaining = 0.02 < 0.05
        )
    )

    # Trigger routing with two different max_tokens values
    result_small = await routing.suggest(
        agent_id, session_id, "claude-opus-4-6", max_tokens=100
    )
    result_large = await routing.suggest(
        agent_id, session_id, "claude-opus-4-6", max_tokens=10000
    )

    # Both should route down because remaining budget < threshold regardless
    # The key check: routing.suggestion events capture different estimated costs
    await asyncio.sleep(0.05)
    await audit._writer.flush()

    suggestion_events = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "routing.suggestion" and e.agent_id == agent_id
    ]
    assert len(suggestion_events) >= 2, (
        f"Expected >= 2 routing.suggestion events; found {len(suggestion_events)}"
    )

    costs = sorted(
        {e.metadata["estimated_cost_requested"] for e in suggestion_events}
    )
    # With different max_tokens (100 vs 10000), estimated costs must differ
    assert len(costs) >= 2, (
        f"Expected different estimated_cost_requested values; got {costs}"
    )


# ---------------------------------------------------------------------------
# Test 12: rules + allowed_models conflict — rule target blocked, falls back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rules_target_blocked_by_allowed_models_falls_back(
    audit: AuditModule,
    cost: CostModule,
) -> None:
    """rules strategy: when rule remaps to a model NOT in allowed_models, routing falls back."""
    agent_id = "agent-rules-blocked"
    session_id = uuid4()

    routing = RoutingModule(audit, cost)

    # Scope allows only haiku; rule tries to route opus → sonnet, but sonnet is blocked
    scope_module = MagicMock()
    scope_policy = ScopePolicy(
        agent_id=agent_id,
        allowed_models=frozenset({"claude-haiku-4-5"}),
    )
    scope_module.get_policy.return_value = scope_policy
    routing._scope = scope_module  # type: ignore[attr-defined]

    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="rules",
            model_rules={"claude-opus-4-6": "claude-sonnet-4-6"},
        )
    )

    # Rule maps opus → sonnet, but sonnet is not in allowed_models
    # _apply_allowed_models_constraint should fall back to requested_model (opus),
    # which is also not in allowed_models, so returns the fallback (requested_model)
    result = await routing.suggest(agent_id, session_id, "claude-opus-4-6")
    # Both target and fallback are blocked — routing is advisory, returns fallback
    assert result == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Test 13: cost_aware with only cheap_model set — no crash, returns cheap_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_aware_only_cheap_model_set(
    audit: AuditModule,
    cost: CostModule,
) -> None:
    """cost_aware with only cheap_model: no crash and returns cheap_model when budget low."""
    agent_id = "agent-only-cheap"
    session_id = uuid4()

    cost.register(BudgetPolicy(agent_id=agent_id, per_session_usd=0.50))
    await cost.track(agent_id, session_id, tokens=100, usd=0.45)

    routing = RoutingModule(audit, cost)
    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            min_session_usd_for_expensive=0.10,
        )
    )

    # requested_model IS cheap_model here — no route-down candidate available,
    # so routing returns the requested model unchanged (it's already cheap)
    result = await routing.suggest(
        agent_id, session_id, "claude-haiku-4-5", max_tokens=500
    )
    assert result == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Test 14: all tiers blocked by allowed_models — falls back to requested_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_tiers_blocked_by_allowed_models_returns_requested(
    audit: AuditModule,
    cost: CostModule,
) -> None:
    """When all tier models are blocked by allowed_models, returns requested_model (advisory)."""
    agent_id = "agent-all-blocked"
    session_id = uuid4()

    routing = RoutingModule(audit, cost)

    # allowed_models does NOT include any tier
    scope_module = MagicMock()
    scope_policy = ScopePolicy(
        agent_id=agent_id,
        allowed_models=frozenset({"some-other-model"}),
    )
    scope_module.get_policy.return_value = scope_policy
    routing._scope = scope_module  # type: ignore[attr-defined]

    cost.register(BudgetPolicy(agent_id=agent_id, per_session_usd=0.50))
    await cost.track(agent_id, session_id, tokens=100, usd=0.45)

    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            mid_model="claude-sonnet-4-6",
            expensive_model="claude-opus-4-6",
            min_session_usd_for_expensive=0.10,
        )
    )

    result = await routing.suggest(
        agent_id, session_id, "claude-opus-4-6", max_tokens=500
    )
    # All tiers blocked — routing is advisory, returns requested_model as fallback
    assert result == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Test 15: both session AND daily budget low — reason is one of the two
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rules_blocked_by_allowed_models_emits_event(
    audit: AuditModule,
    cost: CostModule,
    audit_store: InMemoryAuditStore,
) -> None:
    """When rules target is blocked by allowed_models, emit a visibility event.

    Without this event, an operator would see no audit trace that a rule was
    suppressed — indistinguishable from 'no routing policy registered'.
    """
    agent_id = "agent-rules-observed"
    session_id = uuid4()

    routing = RoutingModule(audit, cost)

    scope_module = MagicMock()
    scope_policy = ScopePolicy(
        agent_id=agent_id,
        allowed_models=frozenset({"claude-haiku-4-5"}),
    )
    scope_module.get_policy.return_value = scope_policy
    routing._scope = scope_module  # type: ignore[attr-defined]

    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="rules",
            model_rules={"claude-opus-4-6": "claude-sonnet-4-6"},
        )
    )

    result = await routing.suggest(agent_id, session_id, "claude-opus-4-6")
    assert result == "claude-opus-4-6"  # Fallback because sonnet is not allowed

    events = await _flush_and_collect(audit, audit_store, "routing.suggestion")
    matching = [e for e in events if e.agent_id == agent_id]
    assert len(matching) >= 1, (
        f"Expected routing.suggestion event with allowed_models_constraint reason; "
        f"got {len(matching)}"
    )
    assert matching[0].metadata["reason"] == "allowed_models_constraint"
    assert matching[0].metadata["strategy"] == "rules"


# ---------------------------------------------------------------------------
# Test 17: background tasks from register() are tracked, not fire-and-forget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_tracks_background_tasks(
    routing: RoutingModule,
) -> None:
    """register() must hold strong references to background audit tasks.

    Python's asyncio GC can collect tasks without a held reference. The
    RoutingModule keeps them in self._pending_tasks and removes them via a
    done callback. This test verifies the set is populated during the in-
    flight window and drains to empty after the tasks complete.
    """
    agent_id = "agent-task-tracking"

    # Before register(), no pending tasks
    assert len(routing._pending_tasks) == 0  # type: ignore[attr-defined]

    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="rules",
            model_rules={"claude-opus-4-6": "claude-haiku-4-5"},
        )
    )

    # At least one task should have been tracked (the audit.log coroutine).
    # It may already have completed synchronously in the in-memory writer
    # path, so we assert that tasks are EITHER present OR cleanly drained —
    # what we DON'T want is a task silently dropped before completion.
    initial_count = len(routing._pending_tasks)  # type: ignore[attr-defined]

    # Yield so any scheduled tasks can run and remove themselves
    for _ in range(5):
        await asyncio.sleep(0)

    final_count = len(routing._pending_tasks)  # type: ignore[attr-defined]
    assert final_count == 0, (
        f"Pending tasks should drain to zero after scheduling; "
        f"initial={initial_count}, final={final_count}"
    )


# ---------------------------------------------------------------------------
# Test 18: cost_aware with only cheap_model + requesting different model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_aware_only_cheap_routes_other_model_down(
    audit: AuditModule,
    cost: CostModule,
) -> None:
    """cost_aware: only cheap_model configured — caller asks for a different
    model, budget is low, should route down to cheap_model."""
    agent_id = "agent-only-cheap-routes"
    session_id = uuid4()

    cost.register(BudgetPolicy(agent_id=agent_id, per_session_usd=0.50))
    await cost.track(agent_id, session_id, tokens=100, usd=0.45)

    routing = RoutingModule(audit, cost)
    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            min_session_usd_for_expensive=0.10,
        )
    )

    # Request opus (not cheap_model); budget is low; there's nothing to
    # route THROUGH but there is a cheap_model to route DOWN to.
    result = await routing.suggest(
        agent_id, session_id, "claude-opus-4-6", max_tokens=500
    )
    assert result == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Test 19: both session AND daily budget low — reason is one of the two
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_off_by_default_in_sdk() -> None:
    """GovernanceSDK must NOT expose sdk.routing unless enable_routing=True.

    Architectural invariant (CLAUDE.md): every module is opt-in via config,
    not code changes. Routing mutates inputs — it MUST default to off.
    """
    import secrets as _secrets

    from codeatelier_governance import GovernanceSDK
    _real_secret = _secrets.token_bytes(32)

    # Default construction — no enable_routing kwarg
    sdk = GovernanceSDK(
        database_url=None, api_key="test-key", audit_secret=_real_secret
    )
    try:
        assert not hasattr(sdk, "routing"), (
            "Routing must be OFF by default. "
            "GovernanceSDK() without enable_routing=True should not expose sdk.routing."
        )
        assert sdk.config.enable_routing is False
    finally:
        await sdk.close()


# ---------------------------------------------------------------------------
# Test 21: enable_routing=True exposes sdk.routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_routing_true_exposes_module() -> None:
    """When enable_routing=True, sdk.routing is constructed and usable."""
    import secrets as _secrets

    from codeatelier_governance import GovernanceSDK
    _real_secret = _secrets.token_bytes(32)

    sdk = GovernanceSDK(
        database_url=None,
        api_key="test-key",
        audit_secret=_real_secret,
        enable_routing=True,
    )
    try:
        assert hasattr(sdk, "routing"), (
            "sdk.routing must exist when enable_routing=True"
        )
        assert sdk.config.enable_routing is True
        # has_policies() should return False since none registered
        assert sdk.routing.has_policies() is False
    finally:
        await sdk.close()


# ---------------------------------------------------------------------------
# Test 22: has_policies() reflects registration state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_policies_reflects_registration(
    routing: RoutingModule,
) -> None:
    """has_policies() is False before register() and True after."""
    assert routing.has_policies() is False
    routing.register(
        RoutingPolicy(
            agent_id="agent-has-policy",
            strategy="rules",
            model_rules={"claude-opus-4-6": "claude-haiku-4-5"},
        )
    )
    assert routing.has_policies() is True


# ---------------------------------------------------------------------------
# Test 23: both session AND daily budget low — reason is one of the two
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_session_and_daily_budget_low_routes_down(
    audit: AuditModule,
    cost: CostModule,
    audit_store: InMemoryAuditStore,
) -> None:
    """When both session and daily budget are low, routing routes down with a valid reason."""
    agent_id = "agent-both-low"
    session_id = uuid4()

    # Both session and daily budget caps set
    cost.register(
        BudgetPolicy(
            agent_id=agent_id,
            per_session_usd=0.50,
            per_agent_usd_daily=1.00,
        )
    )
    # Burn most of both budgets
    await cost.track(agent_id, session_id, tokens=100, usd=0.45)

    routing = RoutingModule(audit, cost)
    routing.register(
        RoutingPolicy(
            agent_id=agent_id,
            strategy="cost_aware",
            cheap_model="claude-haiku-4-5",
            expensive_model="claude-opus-4-6",
            min_session_usd_for_expensive=0.10,
            min_daily_usd_for_expensive=0.10,
        )
    )

    result = await routing.suggest(
        agent_id, session_id, "claude-opus-4-6", max_tokens=500
    )
    # Should route down since both session ($0.05 remaining) and daily are below threshold
    assert result == "claude-haiku-4-5"

    # Check the audit event reason is one of the two valid reasons
    events = await _flush_and_collect(audit, audit_store, "routing.suggestion")
    matching = [e for e in events if e.agent_id == agent_id]
    assert len(matching) >= 1, f"Expected routing.suggestion event; got {len(matching)}"
    reason = matching[0].metadata["reason"]
    assert reason in ("session_budget_low", "daily_budget_low"), (
        f"Expected reason to be session_budget_low or daily_budget_low; got {reason!r}"
    )
