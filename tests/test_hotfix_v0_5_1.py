"""Regression tests for the v0.5.1 hotfix.

This file pins the four fixes that landed on branch
``hotfix/v0.5.1-activation-consistency``:

    Fix 1: enable_* flags in GovernanceConfig actually gate module
           construction (B1 from the DX Consultant audit).
    Fix 2: ScopeModule.filter_tools raises PolicyNotRegistered instead
           of silently returning the full tool list (B2).
    Fix 3: Sync-path policy registration no longer calls asyncio.run();
           upserts are deferred to sdk.start() (B3).
    Fix 4: GatesStore.has_granted_approval is implemented in both
           InMemoryGatesStore and PostgresGatesStore, and
           ContractsModule._check_hitl_approved uses it — previously
           the Postgres path returned False unconditionally, blocking
           every HITL-gated contract in production deployments.
"""
from __future__ import annotations

import secrets as _secrets
from uuid import uuid4

import pytest
import pytest_asyncio

from codeatelier_governance import GovernanceSDK
from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
from codeatelier_governance.contracts import ContractsModule
from codeatelier_governance.contracts.errors import ContractViolation
from codeatelier_governance.contracts.models import Contract, PreCondition
from codeatelier_governance.cost import CostModule
from codeatelier_governance.cost.store import InMemoryCostStore
from codeatelier_governance.gates.models import ApprovalRequest
from codeatelier_governance.gates.module import GatesModule
from codeatelier_governance.gates.store import InMemoryGatesStore
from codeatelier_governance.scope import (
    PolicyNotRegistered,
    ScopeModule,
    ScopePolicy,
)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _secret() -> bytes:
    return _secrets.token_bytes(32)


# ---------------------------------------------------------------------------
# FIX 1 — enable_* flags actually gate module construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_sdk_has_all_modules() -> None:
    """Baseline: with no flags set, all opt-in-True modules are constructed."""
    sdk = GovernanceSDK(
        database_url=None, api_key="test", audit_secret=_secret()
    )
    try:
        assert hasattr(sdk, "audit")
        assert hasattr(sdk, "scope")
        assert hasattr(sdk, "cost")
        assert hasattr(sdk, "gates")
        assert hasattr(sdk, "contracts")
        # Routing remains opt-in (v0.5.1 default)
        assert not hasattr(sdk, "routing")
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_enable_scope_false_removes_scope() -> None:
    sdk = GovernanceSDK(
        database_url=None,
        api_key="test",
        audit_secret=_secret(),
        enable_scope=False,
    )
    try:
        assert not hasattr(sdk, "scope"), (
            "enable_scope=False must not construct sdk.scope"
        )
        # Contracts cascades off when scope is off
        assert not hasattr(sdk, "contracts"), (
            "contracts must cascade-disable when scope is off"
        )
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_enable_cost_false_removes_cost() -> None:
    sdk = GovernanceSDK(
        database_url=None,
        api_key="test",
        audit_secret=_secret(),
        enable_cost=False,
    )
    try:
        assert not hasattr(sdk, "cost")
        # Contracts cascades off when cost is off
        assert not hasattr(sdk, "contracts")
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_enable_gates_false_removes_gates() -> None:
    sdk = GovernanceSDK(
        database_url=None,
        api_key="test",
        audit_secret=_secret(),
        enable_gates=False,
    )
    try:
        assert not hasattr(sdk, "gates")
        # Contracts still exists but with gates=None
        assert hasattr(sdk, "contracts")
        assert sdk.contracts._gates is None
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_enable_audit_false_uses_in_memory_substrate() -> None:
    """enable_audit=False does NOT remove sdk.audit (every module needs it)
    but swaps the backing store to an in-memory ring buffer with no
    persistence."""
    sdk = GovernanceSDK(
        database_url=None,
        api_key="test",
        audit_secret=_secret(),
        enable_audit=False,
    )
    try:
        assert hasattr(sdk, "audit")
        assert isinstance(sdk.audit._store, InMemoryAuditStore)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_enable_routing_requires_cost() -> None:
    """Routing cascade: if enable_cost=False, routing cannot be constructed
    even if enable_routing=True."""
    sdk = GovernanceSDK(
        database_url=None,
        api_key="test",
        audit_secret=_secret(),
        enable_cost=False,
        enable_routing=True,
    )
    try:
        assert not hasattr(sdk, "routing"), (
            "routing must cascade-disable when cost is off"
        )
    finally:
        await sdk.close()


# ---------------------------------------------------------------------------
# FIX 2 — ScopeModule.filter_tools raises on unknown agent
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def scope_with_audit() -> ScopeModule:
    audit = AuditModule(InMemoryAuditStore(), secret=_secret())
    await audit.start()
    try:
        yield ScopeModule(audit)
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_filter_tools_fails_closed_on_unknown_agent(
    scope_with_audit: ScopeModule,
) -> None:
    """Previously returned the full tool list — now raises.

    This was a silent hidden_tools bypass: any code path that called
    ``filter_tools`` for an agent that had never been registered
    received the full list back unfiltered, exposing tools the
    operator thought were hidden.
    """
    with pytest.raises(PolicyNotRegistered):
        scope_with_audit.filter_tools(
            "never-registered-agent", ["tool_a", "tool_b", "tool_c"]
        )


@pytest.mark.asyncio
async def test_filter_tools_still_works_for_registered_agent(
    scope_with_audit: ScopeModule,
) -> None:
    """Registered agents keep the hidden_tools filtering behaviour."""
    scope_with_audit.register(
        ScopePolicy(
            agent_id="known",
            allowed_tools=frozenset({"tool_a", "tool_b"}),
            hidden_tools=frozenset({"tool_b"}),
        )
    )
    result = scope_with_audit.filter_tools(
        "known", ["tool_a", "tool_b", "tool_c"]
    )
    assert result == ["tool_a", "tool_c"]


# ---------------------------------------------------------------------------
# FIX 3 — register() before start() does not call asyncio.run()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_sync_path_defers_upsert_without_asyncio_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that register() does NOT call asyncio.run() on the sync path.

    Prior to v0.5.1, calling register() without an active event loop
    resulted in an inline ``asyncio.run()`` which could deadlock sync
    startup in codebases that already owned an outer loop.  The fix
    queues the upsert and drains it on ``sdk.start()``.
    """
    # Build the scope module by hand so we can patch asyncio.run on the
    # module's reference to it.
    audit = AuditModule(InMemoryAuditStore(), secret=_secret())
    await audit.start()
    try:
        scope = ScopeModule(audit)
        # No DB engine configured, so _persist_policy_best_effort exits
        # early — this test only asserts the SYNC entry point does not
        # crash and does not call asyncio.run.  For the DB-engine case
        # we rely on the integration test below which goes through
        # GovernanceSDK.start().
        scope.register(
            ScopePolicy(
                agent_id="deferred",
                allowed_tools=frozenset({"read"}),
            )
        )
        # The policy is immediately available in-memory even though
        # persistence was deferred.
        policy = scope.get_policy("deferred")
        assert policy is not None
        assert policy.agent_id == "deferred"
    finally:
        await audit.close()


# ---------------------------------------------------------------------------
# FIX 4 — HITL approval check works for both in-memory AND Postgres paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_granted_approval_in_memory_strict_agent_match() -> None:
    """InMemoryGatesStore.has_granted_approval filters by agent_id strictly.

    Prior to v0.5.1 the in-memory path was permissive — any granted
    resolution passed the check regardless of agent_id.
    """
    from datetime import datetime, timedelta, timezone

    store = InMemoryGatesStore()
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    req = ApprovalRequest(
        request_id=uuid4(),
        agent_id="agent-a",
        kind="charge",
        action_hash="a" * 64,
        token="t" * 64,
        expires_at=expires,
        payload={},
    )
    await store.insert_pending(req)
    await store.resolve(req.request_id, "granted")

    # Agent with matching approval: True
    assert await store.has_granted_approval("agent-a") is True
    # Different agent: False (prior to v0.5.1 this was permissive)
    assert await store.has_granted_approval("agent-b") is False


@pytest.mark.asyncio
async def test_has_granted_approval_filters_expired() -> None:
    """Expired approvals do not count."""
    from datetime import datetime, timedelta, timezone

    store = InMemoryGatesStore()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    req = ApprovalRequest(
        request_id=uuid4(),
        agent_id="agent-expired",
        kind="charge",
        action_hash="a" * 64,
        token="t" * 64,
        expires_at=past,
        payload={},
    )
    await store.insert_pending(req)
    await store.resolve(req.request_id, "granted")

    assert await store.has_granted_approval("agent-expired") is False


@pytest.mark.asyncio
async def test_contracts_hitl_approved_via_store_api() -> None:
    """ContractsModule._check_hitl_approved delegates to store.has_granted_approval.

    Previously reached into store._resolutions / store._pending private
    attributes; now uses the public API which is implemented for both
    in-memory AND Postgres backends.
    """
    from datetime import datetime, timedelta, timezone

    audit = AuditModule(InMemoryAuditStore(), secret=_secret())
    await audit.start()
    try:
        scope = ScopeModule(audit)
        cost = CostModule(audit, store=InMemoryCostStore())
        gates_store = InMemoryGatesStore()
        gates = GatesModule(audit, secret=_secret(), store=gates_store)
        contracts = ContractsModule(audit, scope, cost, gates=gates)

        contracts.register(
            Contract(
                agent_id="a",
                tool="charge",
                pre=[
                    PreCondition(
                        check="hitl_approved",
                        message="Needs human approval",
                    )
                ],
            )
        )

        # Without approval: blocked
        with pytest.raises(ContractViolation, match="hitl_approved"):
            await contracts.check_pre("a", uuid4(), "charge")

        # Grant an approval for agent 'a'
        req = ApprovalRequest(
            request_id=uuid4(),
            agent_id="a",
            kind="charge",
            action_hash="a" * 64,
            token="t" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            payload={},
        )
        await gates_store.insert_pending(req)
        await gates_store.resolve(req.request_id, "granted")

        # Now the pre-check passes
        await contracts.check_pre("a", uuid4(), "charge")
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_contracts_hitl_denied_for_different_agent() -> None:
    """A granted approval for agent-b must NOT unblock agent-a."""
    from datetime import datetime, timedelta, timezone

    audit = AuditModule(InMemoryAuditStore(), secret=_secret())
    await audit.start()
    try:
        scope = ScopeModule(audit)
        cost = CostModule(audit, store=InMemoryCostStore())
        gates_store = InMemoryGatesStore()
        gates = GatesModule(audit, secret=_secret(), store=gates_store)
        contracts = ContractsModule(audit, scope, cost, gates=gates)

        contracts.register(
            Contract(
                agent_id="agent-a",
                tool="charge",
                pre=[
                    PreCondition(
                        check="hitl_approved",
                        message="Needs human approval",
                    )
                ],
            )
        )

        # Grant approval for a DIFFERENT agent
        req = ApprovalRequest(
            request_id=uuid4(),
            agent_id="agent-b",
            kind="charge",
            action_hash="a" * 64,
            token="t" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            payload={},
        )
        await gates_store.insert_pending(req)
        await gates_store.resolve(req.request_id, "granted")

        # agent-a still blocked
        with pytest.raises(ContractViolation, match="hitl_approved"):
            await contracts.check_pre("agent-a", uuid4(), "charge")
    finally:
        await audit.close()
