"""Happy-path + exploit tests for the cost (spend limits) module."""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from codeatelier_governance.audit import InMemoryAuditStore
from codeatelier_governance.cost import (
    BudgetExceeded,
    BudgetPolicy,
    CostModule,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_track_then_check_under_budget(cost: CostModule) -> None:
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=1.0))
    sid = uuid4()
    await cost.track("a", sid, usd=0.5)
    await cost.check_or_raise("a", sid)  # under budget


@pytest.mark.asyncio
async def test_session_usd_breach_raises_and_logs(
    cost: CostModule, audit_store: InMemoryAuditStore
) -> None:
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=0.10))
    sid = uuid4()
    await cost.track("a", sid, usd=0.15)
    with pytest.raises(BudgetExceeded, match="per_session_usd"):
        await cost.check_or_raise("a", sid)
    await asyncio.sleep(0.1)
    breaches = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "budget.exceeded"
    ]
    assert len(breaches) == 1
    assert breaches[0].metadata["cap"] == "per_session_usd"
    assert breaches[0].metadata["used"] == 0.15
    assert breaches[0].metadata["limit"] == 0.10


@pytest.mark.asyncio
async def test_session_tokens_breach(cost: CostModule) -> None:
    cost.register(BudgetPolicy(agent_id="a", per_session_tokens=100))
    sid = uuid4()
    await cost.track("a", sid, tokens=150)
    with pytest.raises(BudgetExceeded, match="per_session_tokens"):
        await cost.check_or_raise("a", sid)


@pytest.mark.asyncio
async def test_agent_daily_usd_breach(cost: CostModule) -> None:
    cost.register(BudgetPolicy(agent_id="a", per_agent_usd_daily=1.0))
    s1 = uuid4()
    s2 = uuid4()
    await cost.track("a", s1, usd=0.6)
    await cost.track("a", s2, usd=0.5)
    # Each session is under its own (no per-session cap), but the daily total is 1.1
    with pytest.raises(BudgetExceeded, match="per_agent_usd_daily"):
        await cost.check_or_raise("a", s1)


@pytest.mark.asyncio
async def test_no_policy_means_no_enforcement(cost: CostModule) -> None:
    sid = uuid4()
    await cost.track("ungoverned", sid, usd=999_999)
    await cost.check_or_raise("ungoverned", sid)  # no raise


@pytest.mark.asyncio
async def test_snapshot_returns_remaining(cost: CostModule) -> None:
    cost.register(
        BudgetPolicy(
            agent_id="a", per_session_usd=1.0, per_session_tokens=1000
        )
    )
    sid = uuid4()
    await cost.track("a", sid, usd=0.30, tokens=400)
    snap = await cost.snapshot("a", sid)
    assert snap.session_usd_used == pytest.approx(0.30)
    assert snap.session_tokens_used == 400
    assert snap.session_usd_remaining == pytest.approx(0.70)
    assert snap.session_tokens_remaining == 600


# ---------------------------------------------------------------------------
# Exploit / cybersecurity tests
# ---------------------------------------------------------------------------
def test_policy_with_no_caps_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one cap"):
        BudgetPolicy(agent_id="a")


def test_negative_caps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        BudgetPolicy(agent_id="a", per_session_usd=-1.0)


def test_oversized_caps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        BudgetPolicy(agent_id="a", per_session_usd=10_000_000.0)


def test_policy_is_frozen() -> None:
    p = BudgetPolicy(agent_id="a", per_session_usd=1.0)
    with pytest.raises(ValidationError):
        p.per_session_usd = 9999.0  # type: ignore[misc]


@pytest.mark.asyncio
async def test_track_rejects_negative_delta_silently(cost: CostModule) -> None:
    """Counters are monotonic — agent cannot 'refund' to evade budget.

    Negative deltas are logged-and-dropped (non-breaking observation contract)
    rather than raised (which would break the host call flow). The counter
    must NOT change when a negative delta is rejected.
    """
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=1.0))
    sid = uuid4()
    await cost.track("a", sid, usd=0.5)
    # Must NOT raise — observation surfaces never break the host call.
    await cost.track("a", sid, usd=-0.5)
    snap = await cost.snapshot("a", sid)
    assert snap.session_usd_used == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_concurrent_tracks_are_atomic(cost: CostModule) -> None:
    """200 concurrent track calls should sum correctly with no lost updates."""
    cost.register(BudgetPolicy(agent_id="a", per_agent_usd_daily=1000.0))
    sid = uuid4()
    await asyncio.gather(*(cost.track("a", sid, usd=0.01) for _ in range(200)))
    snap = await cost.snapshot("a", sid)
    assert snap.session_usd_used == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_breach_event_includes_session_id(
    cost: CostModule, audit_store: InMemoryAuditStore
) -> None:
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=0.10))
    sid = uuid4()
    await cost.track("a", sid, usd=0.20)
    with pytest.raises(BudgetExceeded):
        await cost.check_or_raise("a", sid)
    await asyncio.sleep(0.1)
    breach = next(
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "budget.exceeded"
    )
    assert breach.session_id == sid
