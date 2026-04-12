"""Tests for error messages raised by REAL production code paths.

Previous version constructed errors with hand-crafted safe strings and asserted
those were safe -- circular logic. These tests trigger real exceptions from the
SDK modules and verify the messages are clean: no DB URLs, no internal file
paths, no SQL statements.
"""
from __future__ import annotations

import secrets as _secrets
from typing import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.cost.errors import BudgetExceeded
from codeatelier_governance.cost.models import BudgetPolicy
from codeatelier_governance.cost.module import CostModule
from codeatelier_governance.cost.store import CostStore
from codeatelier_governance.errors import GovernanceError
from codeatelier_governance.loop.errors import LoopDetected
from codeatelier_governance.loop.models import LoopPolicy
from codeatelier_governance.loop.module import LoopModule
from codeatelier_governance.scope.errors import PolicyNotRegistered, ScopeViolation
from codeatelier_governance.scope.models import ScopePolicy
from codeatelier_governance.scope.module import ScopeModule

# ---------------------------------------------------------------------------
# Patterns that must NEVER appear in user-facing error messages
# ---------------------------------------------------------------------------
_FORBIDDEN_PATTERNS = [
    "postgresql://",
    "postgres://",
    "password",
    "/Users/",
    "/home/",
    "/src/",
    "/opt/",
    "SELECT ",
    "INSERT ",
    "UPDATE ",
    "DELETE ",
    "asyncpg",
]


def _assert_message_clean(err: Exception) -> None:
    """Assert that the stringified error contains none of the forbidden patterns."""
    msg = str(err).lower()
    for pattern in _FORBIDDEN_PATTERNS:
        assert pattern.lower() not in msg, (
            f"Error message leaks forbidden pattern {pattern!r}: {str(err)!r}"
        )


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def audit_store() -> InMemoryAuditStore:
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def audit(audit_store: InMemoryAuditStore) -> AsyncIterator[AuditModule]:
    writer = BatchingWriter(
        primary=audit_store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    module = AuditModule(
        audit_store, secret=_secrets.token_bytes(32), writer=writer,
    )
    await module.start()
    try:
        yield module
    finally:
        await module.close()


# ---------------------------------------------------------------------------
# 1. Scope violation from real production code
# ---------------------------------------------------------------------------
class TestScopeViolationProductionMessage:
    """Register a real scope policy, call check with a blocked tool,
    catch ScopeViolation, verify the message is clean."""

    @pytest.mark.asyncio
    async def test_scope_violation_production_message(
        self, audit: AuditModule,
    ) -> None:
        scope = ScopeModule(audit)
        scope.register(
            ScopePolicy(
                agent_id="billing-agent",
                allowed_tools=frozenset({"read_invoice", "send_email"}),
            )
        )

        with pytest.raises(ScopeViolation) as exc_info:
            await scope.check("billing-agent", tool="delete_database")

        err = exc_info.value
        _assert_message_clean(err)
        assert isinstance(err, GovernanceError)
        assert err.recovery_hint, "recovery_hint must be non-empty"
        assert "delete_database" in str(err)
        assert "billing-agent" in str(err)


# ---------------------------------------------------------------------------
# 2. Budget exceeded from real production code
# ---------------------------------------------------------------------------
class TestBudgetExceededProductionMessage:
    """Register a budget policy, track over the limit, call check_or_raise,
    catch BudgetExceeded, verify the message is clean."""

    @pytest.mark.asyncio
    async def test_budget_exceeded_production_message(
        self, audit: AuditModule,
    ) -> None:
        cost = CostModule(audit)
        cost.register(
            BudgetPolicy(agent_id="spender", per_session_usd=1.00)
        )
        session_id = uuid4()

        # Push usage over the cap
        await cost.track("spender", session_id, tokens=5000, usd=1.50)

        with pytest.raises(BudgetExceeded) as exc_info:
            await cost.check_or_raise("spender", session_id)

        err = exc_info.value
        _assert_message_clean(err)
        assert isinstance(err, GovernanceError)
        assert err.recovery_hint, "recovery_hint must be non-empty"
        assert "spender" in str(err)


# ---------------------------------------------------------------------------
# 3. Fail-closed from a broken store
# ---------------------------------------------------------------------------
class _BrokenCostStore(CostStore):
    """A cost store that raises on every read -- simulates DB down."""

    async def track(
        self,
        agent_id: str,
        session_id: object,
        *,
        tokens: int,
        usd: float,
        model: str | None = None,
    ) -> None:
        return None  # track never raises by contract

    async def get_session_usage(
        self, agent_id: str, session_id: object,
    ) -> tuple[float, int]:
        raise ConnectionError("simulated DB failure")

    async def get_agent_daily_usage(self, agent_id: str) -> tuple[float, int]:
        raise ConnectionError("simulated DB failure")

    async def close(self) -> None:
        return None


class TestFailClosedProductionMessage:
    """Create a CostModule with a broken store, call check_or_raise,
    catch BudgetExceeded, verify message mentions fail_open but does NOT
    contain a connection URL or SQL statement."""

    @pytest.mark.asyncio
    async def test_fail_closed_production_message(
        self, audit: AuditModule,
    ) -> None:
        broken_store = _BrokenCostStore()
        cost = CostModule(
            audit,
            store=broken_store,
            fail_open=False,
        )
        cost.register(
            BudgetPolicy(agent_id="careful-agent", per_session_usd=10.0)
        )
        session_id = uuid4()

        with pytest.raises(BudgetExceeded) as exc_info:
            await cost.check_or_raise("careful-agent", session_id)

        err = exc_info.value
        _assert_message_clean(err)
        assert "fail_open" in str(err), (
            "Message should mention fail_open so the operator knows how to override"
        )
        assert err.recovery_hint, "recovery_hint must be non-empty"


# ---------------------------------------------------------------------------
# 4. PolicyNotRegistered from real production code
# ---------------------------------------------------------------------------
class TestPolicyNotRegisteredProductionMessage:
    """Call scope.check for an unknown agent, catch PolicyNotRegistered,
    verify the message is clean."""

    @pytest.mark.asyncio
    async def test_policy_not_registered_production_message(
        self, audit: AuditModule,
    ) -> None:
        scope = ScopeModule(audit)
        # Do NOT register any policy for "ghost-agent"

        with pytest.raises(PolicyNotRegistered) as exc_info:
            await scope.check("ghost-agent", tool="anything")

        err = exc_info.value
        _assert_message_clean(err)
        assert isinstance(err, GovernanceError)
        assert err.recovery_hint, "recovery_hint must be non-empty"
        assert "ghost-agent" in str(err)


# ---------------------------------------------------------------------------
# 5. LoopDetected from real production code
# ---------------------------------------------------------------------------
class TestLoopDetectedProductionMessage:
    """Register a loop policy, fire enough record_call to trigger,
    catch LoopDetected, verify no leakage."""

    @pytest.mark.asyncio
    async def test_loop_detected_production_message(
        self, audit: AuditModule,
    ) -> None:
        loop_mod = LoopModule(audit)
        loop_mod.register(
            LoopPolicy(
                agent_id="loopy",
                window_seconds=60,
                max_calls=3,
                action="raise",
            )
        )
        session_id = uuid4()

        # Fire enough calls to exceed max_calls (3) -> 4th call triggers
        for _ in range(3):
            await loop_mod.record_call("loopy", session_id, "read_file")

        with pytest.raises(LoopDetected) as exc_info:
            await loop_mod.record_call("loopy", session_id, "read_file")

        err = exc_info.value
        _assert_message_clean(err)
        assert isinstance(err, GovernanceError)
        assert "loopy" in str(err)
        assert "read_file" in str(err)
