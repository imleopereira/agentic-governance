"""v0.6.2 Track C Bug #4 — routing preserves Opus 4.7.

Context: routing/models.py previously hardcoded ``claude-opus-4-6`` as a
rewrite source in the docstring example. Any host copying the docstring
verbatim would silently route 4.7 through unchanged (which is fine if
4.7 is what they want). This test proves the cost_aware strategy, when
configured to route DOWN from Opus 4.7, preserves that behaviour — i.e.
4.7 is a first-class expensive-tier model, not a stranger the routing
module ignores.
"""
from __future__ import annotations

import secrets
from uuid import uuid4

import pytest
import pytest_asyncio

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.cost import BudgetPolicy, CostModule
from codeatelier_governance.cost.store import InMemoryCostStore
from codeatelier_governance.routing.models import RoutingPolicy
from codeatelier_governance.routing.module import RoutingModule


@pytest_asyncio.fixture
async def audit_store() -> InMemoryAuditStore:
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def audit(audit_store: InMemoryAuditStore) -> AuditModule:
    writer = BatchingWriter(
        primary=audit_store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    module = AuditModule(
        audit_store, secret=secrets.token_bytes(32), writer=writer,
    )
    await module.start()
    try:
        yield module  # type: ignore[misc]
    finally:
        await module.close()


@pytest_asyncio.fixture
async def cost(audit: AuditModule) -> CostModule:
    return CostModule(audit, store=InMemoryCostStore())


@pytest_asyncio.fixture
async def routing(audit: AuditModule, cost: CostModule) -> RoutingModule:
    return RoutingModule(audit, cost)


@pytest.mark.asyncio
async def test_bug4_routing_policy_with_opus_4_7_as_expensive_tier(
    routing: RoutingModule, cost: CostModule,
) -> None:
    """cost_aware: expensive_model=claude-opus-4-7 routes down when budget low."""
    policy = RoutingPolicy(
        agent_id="route47",
        strategy="cost_aware",
        cheap_model="claude-haiku-4-5",
        mid_model="claude-sonnet-4-6",
        expensive_model="claude-opus-4-7",
        min_session_usd_for_expensive=5.0,
    )
    routing.register(policy)
    cost.register(BudgetPolicy(agent_id="route47", per_session_usd=10.0))
    sid = uuid4()
    # Push session usage high enough that remaining < 5.0.
    await cost.track("route47", sid, usd=7.0)
    selected = await routing.suggest("route47", sid, "claude-opus-4-7")
    assert selected in ("claude-sonnet-4-6", "claude-haiku-4-5")


@pytest.mark.asyncio
async def test_bug4_routing_rule_maps_opus_4_7_to_sonnet(
    routing: RoutingModule, cost: CostModule,
) -> None:
    """rules strategy: claude-opus-4-7 → claude-sonnet-4-6 explicit map."""
    policy = RoutingPolicy(
        agent_id="rule47",
        strategy="rules",
        model_rules={"claude-opus-4-7": "claude-sonnet-4-6"},
    )
    routing.register(policy)
    sid = uuid4()
    selected = await routing.suggest("rule47", sid, "claude-opus-4-7")
    assert selected == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_bug4_routing_rule_preserves_opus_4_7_when_no_match(
    routing: RoutingModule, cost: CostModule,
) -> None:
    """rules strategy without an explicit 4-7 rule: 4-7 passes through unchanged."""
    # Host maps 4-6 but not 4-7 — 4-7 must be preserved, not silently rewritten.
    policy = RoutingPolicy(
        agent_id="rule46only",
        strategy="rules",
        model_rules={"claude-opus-4-6": "claude-sonnet-4-6"},
    )
    routing.register(policy)
    sid = uuid4()
    selected = await routing.suggest("rule46only", sid, "claude-opus-4-7")
    assert selected == "claude-opus-4-7"
