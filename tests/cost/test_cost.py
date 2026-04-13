"""Happy-path + exploit tests for the cost (spend limits) module."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from codeatelier_governance.audit import AuditModule

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
    cost: CostModule, audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=0.10))
    sid = uuid4()
    await cost.track("a", sid, usd=0.15)
    with pytest.raises(BudgetExceeded, match="per_session_usd"):
        await cost.check_or_raise("a", sid)
    await audit._writer.flush()
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
    cost: CostModule, audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=0.10))
    sid = uuid4()
    await cost.track("a", sid, usd=0.20)
    with pytest.raises(BudgetExceeded):
        await cost.check_or_raise("a", sid)
    await audit._writer.flush()
    breach = next(
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "budget.exceeded"
    )
    assert breach.session_id == sid


# ---------------------------------------------------------------------------
# G15: Session time limits
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_session_under_time_limit_passes(cost: CostModule) -> None:
    """Session within time limit should pass check_or_raise."""
    cost.register(BudgetPolicy(agent_id="a", per_session_seconds=300))
    sid = uuid4()
    await cost.track("a", sid, tokens=1)
    await cost.check_or_raise("a", sid)


@pytest.mark.asyncio
async def test_session_over_time_limit_raises(cost: CostModule) -> None:
    """Session exceeding time limit should raise BudgetExceeded."""
    cost.register(BudgetPolicy(agent_id="a", per_session_seconds=10))
    sid = uuid4()
    await cost.track("a", sid, tokens=1)
    # Backdate the session start time to simulate elapsed time
    store = cost._store  # type: ignore[attr-defined]
    key = ("a", sid)
    store._session_started[key] = datetime.now(timezone.utc) - timedelta(seconds=15)
    with pytest.raises(BudgetExceeded, match="(?i)session time limit exceeded"):
        await cost.check_or_raise("a", sid)


@pytest.mark.asyncio
async def test_session_time_limit_logs_audit_event(
    cost: CostModule, audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    """Time limit breach should log a budget.exceeded audit event."""
    cost.register(BudgetPolicy(agent_id="a", per_session_seconds=5))
    sid = uuid4()
    await cost.track("a", sid, tokens=1)
    store = cost._store  # type: ignore[attr-defined]
    store._session_started[("a", sid)] = datetime.now(timezone.utc) - timedelta(seconds=10)
    with pytest.raises(BudgetExceeded):
        await cost.check_or_raise("a", sid)
    await audit._writer.flush()
    breaches = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "budget.exceeded" and e.metadata.get("cap") == "per_session_seconds"
    ]
    assert len(breaches) == 1
    assert breaches[0].metadata["limit"] == 5.0


@pytest.mark.asyncio
async def test_session_no_time_limit_ignores_check(cost: CostModule) -> None:
    """Policy without per_session_seconds should not check time."""
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=100.0))
    sid = uuid4()
    await cost.track("a", sid, tokens=1)
    await cost.check_or_raise("a", sid)


def test_per_session_seconds_field_validation() -> None:
    """per_session_seconds must be 0..86400."""
    BudgetPolicy(agent_id="a", per_session_seconds=300)
    BudgetPolicy(agent_id="a", per_session_seconds=86400)
    with pytest.raises(ValidationError):
        BudgetPolicy(agent_id="a", per_session_seconds=86401)
    with pytest.raises(ValidationError):
        BudgetPolicy(agent_id="a", per_session_seconds=-1)


# ---------------------------------------------------------------------------
# Gap #5: Combined budget query — get_session_and_daily_usage
# ---------------------------------------------------------------------------


class _CombinedQueryStore:
    """Fake store that has get_session_and_daily_usage like PostgresCostStore."""

    def __init__(self) -> None:
        self._session: dict[tuple[str, str], tuple[float, int]] = {}
        self._daily: dict[str, tuple[float, int]] = {}
        self.combined_called = False

    async def track(
        self,
        agent_id: str,
        session_id: "uuid4",  # type: ignore[valid-type]
        *,
        tokens: int = 0,
        usd: float = 0.0,
        model: str | None = None,
    ) -> None:
        key = (agent_id, str(session_id))
        cur_usd, cur_tok = self._session.get(key, (0.0, 0))
        self._session[key] = (cur_usd + usd, cur_tok + tokens)
        d_usd, d_tok = self._daily.get(agent_id, (0.0, 0))
        self._daily[agent_id] = (d_usd + usd, d_tok + tokens)

    async def get_session_usage(
        self, agent_id: str, session_id: "uuid4",  # type: ignore[valid-type]
    ) -> tuple[float, int]:
        return self._session.get((agent_id, str(session_id)), (0.0, 0))

    async def get_agent_daily_usage(self, agent_id: str) -> tuple[float, int]:
        return self._daily.get(agent_id, (0.0, 0))

    async def get_session_and_daily_usage(
        self, agent_id: str, session_id: "uuid4",  # type: ignore[valid-type]
    ) -> tuple[float, int, float, int]:
        self.combined_called = True
        s_usd, s_tok = await self.get_session_usage(agent_id, session_id)
        d_usd, d_tok = await self.get_agent_daily_usage(agent_id)
        return (s_usd, s_tok, d_usd, d_tok)

    async def get_session_start_time(self, *_args: object) -> None:
        return None

    async def get_session_elapsed_seconds(self, *_args: object) -> float | None:
        return None

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_combined_query_used_when_available(audit: "AuditModule") -> None:
    """CostModule.check_or_raise should use get_session_and_daily_usage
    when the store supports it (PostgresCostStore), doing 1 round-trip.
    """

    store = _CombinedQueryStore()
    cost = CostModule(audit, store=store)  # type: ignore[arg-type]
    cost.register(BudgetPolicy(agent_id="combo", per_session_usd=10.0, per_agent_usd_daily=20.0))

    sid = uuid4()
    await store.track("combo", sid, tokens=100, usd=0.50)

    await cost.check_or_raise("combo", sid)

    # Verify the combined method was called (not the two separate ones)
    assert store.combined_called is True


# ---------------------------------------------------------------------------
# Exploit: concurrent track+check_or_raise at budget boundary
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_check_or_raise_at_boundary(cost: CostModule) -> None:
    """Adversarial race: concurrent track+check interleaving at the boundary.

    Setup: $1.00 budget, $0.95 already spent.
    Attack: 10 concurrent coroutines each track($0.10) then check_or_raise().
    Expected:
      - At LEAST some check_or_raise calls must raise BudgetExceeded
        (since $0.95 + $0.10 = $1.05 > $1.00 after the very first track).
      - The total tracked amount must never exceed $1.95
        ($0.95 initial + 10 * $0.10) — counters are monotonic.
    """
    cost.register(BudgetPolicy(agent_id="racer", per_session_usd=1.00))
    sid = uuid4()
    await cost.track("racer", sid, usd=0.95)

    async def track_then_check() -> None:
        """Single attacker coroutine: spend $0.10 then check budget."""
        await cost.track("racer", sid, usd=0.10)
        await cost.check_or_raise("racer", sid)

    results = await asyncio.gather(
        *(track_then_check() for _ in range(10)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BudgetExceeded)]
    # After the first track, total is $1.05 > $1.00 — at least some must fail
    assert len(exceptions) >= 1, (
        "Expected at least one BudgetExceeded but all 10 concurrent "
        "track+check calls passed. The race condition was not detected."
    )

    # Counters are monotonic — total must be exactly $0.95 + 10 * $0.10 = $1.95
    snap = await cost.snapshot("racer", sid)
    assert snap.session_usd_used <= 1.95 + 1e-9, (
        f"Counter exceeded theoretical max: {snap.session_usd_used}"
    )
    assert snap.session_usd_used >= 0.95, (
        f"Counter lost updates: {snap.session_usd_used}"
    )


@pytest.mark.asyncio
async def test_combined_query_returns_correct_values() -> None:
    """The combined query should return consistent session+daily values."""
    store = _CombinedQueryStore()
    sid = uuid4()

    await store.track("agent-x", sid, tokens=500, usd=1.50)
    await store.track("agent-x", sid, tokens=300, usd=0.75)

    s_usd, s_tok, d_usd, d_tok = await store.get_session_and_daily_usage("agent-x", sid)
    assert s_usd == pytest.approx(2.25)
    assert s_tok == 800
    assert d_usd == pytest.approx(2.25)
    assert d_tok == 800


# ---------------------------------------------------------------------------
# Item 3: projected_tokens parameter on check_or_raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_or_raise_projected_tokens_blocks_call(cost: CostModule) -> None:
    """check_or_raise with projected_tokens blocks when current + projected > limit."""
    cost.register(BudgetPolicy(agent_id="p", per_session_tokens=1000))
    sid = uuid4()
    await cost.track("p", sid, tokens=800)

    # 800 current + 300 projected = 1100 > 1000 → should raise
    with pytest.raises(BudgetExceeded, match="per_session_tokens"):
        await cost.check_or_raise("p", sid, projected_tokens=300)


@pytest.mark.asyncio
async def test_check_or_raise_projected_tokens_under_limit_passes(cost: CostModule) -> None:
    """check_or_raise with projected_tokens passes when current + projected <= limit."""
    cost.register(BudgetPolicy(agent_id="p2", per_session_tokens=1000))
    sid = uuid4()
    await cost.track("p2", sid, tokens=800)

    # 800 current + 100 projected = 900 <= 1000 → should pass
    await cost.check_or_raise("p2", sid, projected_tokens=100)


@pytest.mark.asyncio
async def test_check_or_raise_projected_tokens_none_uses_old_behavior(cost: CostModule) -> None:
    """check_or_raise without projected_tokens still checks balance vs limit."""
    cost.register(BudgetPolicy(agent_id="p3", per_session_tokens=1000))
    sid = uuid4()
    await cost.track("p3", sid, tokens=900)

    # 900 < 1000 → passes without projected_tokens
    await cost.check_or_raise("p3", sid)

    await cost.track("p3", sid, tokens=200)  # now 1100
    with pytest.raises(BudgetExceeded, match="per_session_tokens"):
        await cost.check_or_raise("p3", sid)


@pytest.mark.asyncio
async def test_check_or_raise_projected_tokens_daily_cap(cost: CostModule) -> None:
    """check_or_raise projects tokens against daily cap too."""
    cost.register(BudgetPolicy(agent_id="p4", per_agent_tokens_daily=1000))
    s1 = uuid4()
    await cost.track("p4", s1, tokens=800)

    # 800 daily + 300 projected = 1100 > 1000 → blocks
    with pytest.raises(BudgetExceeded, match="per_agent_tokens_daily"):
        await cost.check_or_raise("p4", s1, projected_tokens=300)
