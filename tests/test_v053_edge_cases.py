"""Edge-case tests for v0.5.3 security and architectural fixes.

Covers Issues 1-11 from the v0.5.3 code review:
  1. Scope check unknown exception propagation
  2. Double-wrap guard (_registered_wrappers de-duplication)
  3. projected_tokens exactly at limit and one-over-limit
  4. verify_chain() on empty audit log
  5. verify_chain(from_seq=5, to_seq=3) raises ValueError
  6. coverage_caveat whitespace-only raises ValidationError
  7. agent_id="" raises ValueError
  8. max_tokens=0 treated as absent
  9. default_max_tokens=-1 raises ValueError
  10. coverage_pct=1.5 raises ValidationError
  11. Chain deletion detection
"""
from __future__ import annotations

import secrets
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError

from codeatelier_governance.audit import AuditEvent, AuditModule
from codeatelier_governance.audit.errors import ChainIntegrityError
from codeatelier_governance.audit.store import BatchingWriter, InMemoryAuditStore
from codeatelier_governance.compliance.models import COVERAGE_CAVEAT, ComplianceReport
from codeatelier_governance.cost.models import BudgetPolicy
from codeatelier_governance.cost.module import CostModule
from codeatelier_governance.integrations.anthropic_wrap import (
    wrap_anthropic,
    _resolve_projected_tokens,
)
from codeatelier_governance.scope.errors import ScopeViolation
from codeatelier_governance.scope.models import ScopePolicy
from codeatelier_governance.scope.module import ScopeModule
from codeatelier_governance.sdk import GovernanceConfig


# ---------------------------------------------------------------------------
# Shared fakes (mirrors test_anthropic_wrap.py patterns)
# ---------------------------------------------------------------------------


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(self, model: str = "claude-sonnet-4-6", usage: FakeUsage | None = None) -> None:
        self.model = model
        self.usage = usage


class FakeAsyncMessages:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    async def create(self, **kwargs: Any) -> FakeResponse:
        return self._response


class FakeAsyncClient:
    def __init__(self, response: FakeResponse) -> None:
        self.messages = FakeAsyncMessages(response)


class FakeSDK:
    """Minimal SDK stand-in that mirrors the real interface."""

    def __init__(self, audit: AuditModule, scope: ScopeModule, cost: CostModule) -> None:
        self.audit = audit
        self.scope = scope
        self.cost = cost
        self._registered_wrappers: list[str] = []
        self._started = False


@pytest_asyncio.fixture
async def secret_bytes() -> bytes:
    return secrets.token_bytes(32)


@pytest_asyncio.fixture
async def store() -> InMemoryAuditStore:
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def audit(store: InMemoryAuditStore, secret_bytes: bytes) -> AuditModule:
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02, buffer_max=1000)
    module = AuditModule(store, secret=secret_bytes, writer=writer)
    await module.start()
    yield module  # type: ignore[misc]
    await module.close()


@pytest_asyncio.fixture
async def sdk(audit: AuditModule) -> FakeSDK:
    scope = ScopeModule(audit)
    cost = CostModule(audit)
    return FakeSDK(audit=audit, scope=scope, cost=cost)


# ---------------------------------------------------------------------------
# Test 1: Scope check with unknown exception propagates out of wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_unknown_exception_propagates(sdk: FakeSDK) -> None:
    """A RuntimeError raised by scope.check() must propagate unchanged."""
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    # Register a scope policy so the scope check fires (not skipped)
    sdk.scope.register(
        ScopePolicy(
            agent_id="scope-agent",
            allowed_tools=frozenset({"messages.create"}),
        )
    )

    # Monkey-patch scope.check to raise a RuntimeError
    async def _failing_check(agent_id: str, *, tool: str) -> None:
        raise RuntimeError("unexpected scope backend failure")

    sdk.scope.check = _failing_check  # type: ignore[method-assign]

    wrap_anthropic(client, sdk=sdk, agent_id="scope-agent")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="unexpected scope backend failure"):
        await client.messages.create(model="claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Test 2: Double-wrap guard — _registered_wrappers doesn't double-count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_wrap_no_duplicate_in_registered_wrappers(sdk: FakeSDK) -> None:
    """wrap_anthropic called twice on the same client must not add a duplicate entry."""
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    # Attach _registered_wrappers to sdk so the registration code can use it
    sdk._registered_wrappers = []

    wrap_anthropic(client, sdk=sdk, agent_id="dup-agent")  # type: ignore[arg-type]
    count_after_first = sdk._registered_wrappers.count("anthropic:dup-agent")
    assert count_after_first == 1

    wrap_anthropic(client, sdk=sdk, agent_id="dup-agent")  # type: ignore[arg-type]
    count_after_second = sdk._registered_wrappers.count("anthropic:dup-agent")
    # Must still be 1, not 2
    assert count_after_second == 1


# ---------------------------------------------------------------------------
# Test 3a: projected_tokens exactly at limit — call proceeds
# Test 3b: projected_tokens one over limit — call is blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projected_tokens_exactly_at_limit_proceeds(sdk: FakeSDK) -> None:
    """When current + projected == limit exactly, the call must proceed."""
    sdk.cost.register(
        BudgetPolicy(agent_id="budget-agent", per_session_tokens=1000)
    )
    session_id = uuid4()
    # Use 900 tokens, leaving 100
    await sdk.cost.track("budget-agent", session_id, tokens=900, usd=0.0)

    response = FakeResponse(usage=FakeUsage(50, 50))
    client = FakeAsyncClient(response)
    # Pass the same session_id so budget tracking and gate share the same session
    wrap_anthropic(client, sdk=sdk, agent_id="budget-agent", session_id=session_id)  # type: ignore[arg-type]

    # max_tokens=100 => current(900) + projected(100) == limit(1000), should pass
    result = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
    )
    assert result is response


@pytest.mark.asyncio
async def test_projected_tokens_one_over_limit_blocked(sdk: FakeSDK, store: InMemoryAuditStore) -> None:
    """When current + projected > limit, the call must raise BudgetExceeded."""
    from codeatelier_governance.cost.errors import BudgetExceeded

    sdk.cost.register(
        BudgetPolicy(agent_id="budget-agent2", per_session_tokens=1000)
    )
    session_id = uuid4()
    # Use 900 tokens, leaving 100; request 101 => blocked (900 + 101 = 1001 > 1000)
    await sdk.cost.track("budget-agent2", session_id, tokens=900, usd=0.0)

    response = FakeResponse(usage=FakeUsage(50, 51))
    client = FakeAsyncClient(response)
    # Pass the same session_id so budget tracking and gate share the same session
    wrap_anthropic(client, sdk=sdk, agent_id="budget-agent2", session_id=session_id)  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded):
        await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=101,
        )

    # No llm.call event should be present (budget gate fired before audit)
    all_events = list(store._events.values())
    call_events = [e for e in all_events if e.kind == "llm.call"]
    assert len(call_events) == 0


# ---------------------------------------------------------------------------
# Test 4: verify_chain() on empty audit log returns True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_chain_empty_log_returns_true(audit: AuditModule) -> None:
    """verify_chain() with no events must return True (vacuously valid)."""
    result = await audit.verify_chain()
    assert result is True


# ---------------------------------------------------------------------------
# Test 5: verify_chain(from_seq=5, to_seq=3) raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_chain_invalid_range_raises(audit: AuditModule, store: InMemoryAuditStore) -> None:
    """verify_chain(from_seq=5, to_seq=3) where from > to must raise ValueError."""
    session_id = uuid4()
    for i in range(6):
        await audit.log(
            AuditEvent(session_id=session_id, agent_id="agt", kind=f"ev.{i}")
        )
    await audit._writer.flush()

    with pytest.raises(ValueError):
        await audit.verify_chain(session_id=session_id, from_seq=5, to_seq=3)


# ---------------------------------------------------------------------------
# Test 6: coverage_caveat whitespace-only raises ValidationError
# ---------------------------------------------------------------------------


def test_coverage_caveat_whitespace_raises() -> None:
    """A whitespace-only coverage_caveat must raise ValidationError."""
    with pytest.raises(ValidationError):
        ComplianceReport(
            report_id=uuid4(),
            generated_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            format="article12",
            sections=[],
            chain_integrity_status="unverified",
            coverage_caveat="   ",
            coverage_pct=None,
        )


# ---------------------------------------------------------------------------
# Test 7: agent_id="" raises ValueError
# ---------------------------------------------------------------------------


def test_wrap_anthropic_empty_agent_id_raises(sdk: FakeSDK) -> None:
    """wrap_anthropic with agent_id='' must raise ValueError immediately."""
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    with pytest.raises(ValueError, match="agent_id must be a non-empty string"):
        wrap_anthropic(client, sdk=sdk, agent_id="")  # type: ignore[arg-type]


def test_wrap_anthropic_whitespace_agent_id_raises(sdk: FakeSDK) -> None:
    """wrap_anthropic with agent_id='   ' must raise ValueError immediately."""
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    with pytest.raises(ValueError, match="agent_id must be a non-empty string"):
        wrap_anthropic(client, sdk=sdk, agent_id="   ")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test 8: max_tokens=0 treated as absent (projection not applied as 0)
# ---------------------------------------------------------------------------


def test_resolve_projected_tokens_zero_treated_as_absent() -> None:
    """_resolve_projected_tokens must treat max_tokens=0 as absent (return None)."""

    class _MinimalSDK:
        config = None

    result = _resolve_projected_tokens(_MinimalSDK(), {"max_tokens": 0})
    # max_tokens=0 is meaningless; no default; should return None
    assert result is None


def test_resolve_projected_tokens_negative_treated_as_absent() -> None:
    """_resolve_projected_tokens must treat max_tokens=-5 as absent (return None)."""

    class _MinimalSDK:
        config = None

    result = _resolve_projected_tokens(_MinimalSDK(), {"max_tokens": -5})
    assert result is None


def test_resolve_projected_tokens_positive_respected() -> None:
    """_resolve_projected_tokens must honour positive max_tokens values."""

    class _MinimalSDK:
        config = None

    result = _resolve_projected_tokens(_MinimalSDK(), {"max_tokens": 500})
    assert result == 500


# ---------------------------------------------------------------------------
# Test 9: default_max_tokens=-1 on GovernanceConfig raises ValueError
# ---------------------------------------------------------------------------


def test_governance_config_negative_default_max_tokens_raises() -> None:
    """GovernanceConfig with default_max_tokens=-1 must raise ValueError."""
    with pytest.raises(ValueError, match="default_max_tokens"):
        GovernanceConfig(default_max_tokens=-1)


def test_governance_config_zero_default_max_tokens_raises() -> None:
    """GovernanceConfig with default_max_tokens=0 must raise ValueError."""
    with pytest.raises(ValueError, match="default_max_tokens"):
        GovernanceConfig(default_max_tokens=0)


def test_governance_config_positive_default_max_tokens_ok() -> None:
    """GovernanceConfig with default_max_tokens=1000 must not raise."""
    cfg = GovernanceConfig(default_max_tokens=1000)
    assert cfg.default_max_tokens == 1000


# ---------------------------------------------------------------------------
# Test 10: coverage_pct=1.5 raises ValidationError
# ---------------------------------------------------------------------------


def test_coverage_pct_over_one_raises() -> None:
    """coverage_pct > 1.0 must raise ValidationError."""
    with pytest.raises(ValidationError):
        ComplianceReport(
            report_id=uuid4(),
            generated_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            format="article12",
            sections=[],
            chain_integrity_status="unverified",
            coverage_caveat=COVERAGE_CAVEAT,
            coverage_pct=1.5,
        )


def test_coverage_pct_negative_raises() -> None:
    """coverage_pct < 0.0 must raise ValidationError."""
    with pytest.raises(ValidationError):
        ComplianceReport(
            report_id=uuid4(),
            generated_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            format="article12",
            sections=[],
            chain_integrity_status="unverified",
            coverage_caveat=COVERAGE_CAVEAT,
            coverage_pct=-0.2,
        )


def test_coverage_pct_valid_range_ok() -> None:
    """coverage_pct=0.75 must not raise."""
    report = ComplianceReport(
        report_id=uuid4(),
        generated_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        format="article12",
        sections=[],
        chain_integrity_status="unverified",
        coverage_caveat=COVERAGE_CAVEAT,
        coverage_pct=0.75,
    )
    assert report.coverage_pct == 0.75


# ---------------------------------------------------------------------------
# Test 11: Chain deletion detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_chain_detects_deleted_middle_event(
    audit: AuditModule, store: InMemoryAuditStore
) -> None:
    """Deleting the middle event from a 3-event chain must raise ChainIntegrityError."""
    session_id = uuid4()

    # Build a chain of 3 events
    for i in range(3):
        await audit.log(
            AuditEvent(session_id=session_id, agent_id="del-agent", kind=f"step.{i}")
        )
    await audit._writer.flush()

    # Confirm 3 events in the session
    event_ids = list(store._by_session[session_id])
    assert len(event_ids) == 3

    # Delete the middle event directly from the store
    middle_id = event_ids[1]
    del store._events[middle_id]
    store._by_session[session_id].remove(middle_id)

    # verify_chain() must detect the gap
    with pytest.raises(ChainIntegrityError):
        await audit.verify_chain(session_id=session_id)
