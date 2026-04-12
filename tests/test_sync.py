"""Tests for GovernanceSDKSync — the synchronous wrapper."""
from __future__ import annotations

from uuid import uuid4

from codeatelier_governance import GovernanceSDKSync
from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.cost.models import BudgetPolicy
from codeatelier_governance.scope.errors import ScopeViolation
from codeatelier_governance.scope.models import ScopePolicy


SDK_KWARGS: dict[str, str] = {"api_key": "test-key-for-sync-tests"}


class TestSyncLifecycle:
    """Basic lifecycle: init, start, close."""

    def test_sync_init_close(self) -> None:
        sdk = GovernanceSDKSync(**SDK_KWARGS)
        # Thread should be alive after init
        assert sdk._thread.is_alive()
        assert sdk._thread.daemon is True
        sdk.start()
        sdk.close()
        # Idempotent close
        sdk.close()

    def test_sync_context_manager(self) -> None:
        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            assert sdk._thread.is_alive()
        # After exiting, the loop is stopped
        assert sdk._closed is True


class TestSyncAudit:
    """Audit module through the sync wrapper."""

    def test_sync_audit_log(self) -> None:
        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            event = AuditEvent(agent_id="test-agent", kind="tool.call")
            record = sdk.audit.log(event)
            assert record.agent_id == "test-agent"
            assert record.kind == "tool.call"
            assert record.event_id is not None

    def test_sync_audit_trace(self) -> None:
        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            event = AuditEvent(
                agent_id="test-agent",
                kind="tool.call",
                metadata={"tool": "read_file"},
            )
            record = sdk.audit.log(event)

            chain = sdk.audit.trace(record.event_id)
            assert len(chain) >= 1
            assert chain[0].event_id == record.event_id

    def test_sync_audit_trace_session_chain(self) -> None:
        session_id = uuid4()
        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            for i in range(3):
                sdk.audit.log(
                    AuditEvent(
                        agent_id="test-agent",
                        kind="tool.call",
                        session_id=session_id,
                        metadata={"step": i},
                    )
                )
            chain = sdk.audit.trace_session_chain(session_id)
            assert len(chain) == 3


class TestSyncScope:
    """Scope module through the sync wrapper."""

    def test_sync_scope_check(self) -> None:
        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            policy = ScopePolicy(
                agent_id="agent-1",
                allowed_tools=frozenset({"read_file", "write_file"}),
            )
            sdk.scope.register(policy)

            # Allowed tool: no exception
            sdk.scope.check("agent-1", tool="read_file")

            # Blocked tool: raises ScopeViolation
            import pytest as _pt

            with _pt.raises(ScopeViolation):
                sdk.scope.check("agent-1", tool="delete_db")


class TestSyncCost:
    """Cost module through the sync wrapper."""

    def test_sync_cost_track(self) -> None:
        session_id = uuid4()
        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            policy = BudgetPolicy(
                agent_id="agent-1",
                per_session_usd=10.0,
            )
            sdk.cost.register(policy)

            sdk.cost.track(
                "agent-1", session_id, tokens=500, usd=0.01,
            )

            snap = sdk.cost.snapshot("agent-1", session_id)
            assert snap.session_usd_used >= 0.01
            assert snap.session_tokens_used >= 500


class TestSyncErrorPropagation:
    """Gap #4: Async exceptions must propagate to the sync calling thread."""

    def test_scope_violation_propagates(self) -> None:
        """ScopeViolation raised in async should surface in the sync thread."""
        import pytest as _pt

        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            policy = ScopePolicy(
                agent_id="err-agent",
                allowed_tools=frozenset({"safe_tool"}),
            )
            sdk.scope.register(policy)

            with _pt.raises(ScopeViolation, match="not allowed"):
                sdk.scope.check("err-agent", tool="dangerous_tool")

    def test_budget_exceeded_propagates(self) -> None:
        """BudgetExceeded raised in async should surface in the sync thread."""
        from codeatelier_governance.cost.errors import BudgetExceeded
        import pytest as _pt

        sid = uuid4()
        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            policy = BudgetPolicy(
                agent_id="budget-err-agent",
                per_agent_usd_daily=0.001,
            )
            sdk.cost.register(policy)
            sdk.cost.track("budget-err-agent", sid, tokens=0, usd=1.0)

            with _pt.raises(BudgetExceeded):
                sdk.cost.check_or_raise("budget-err-agent", sid)

    def test_value_error_propagates(self) -> None:
        """ValueError from the async SDK should propagate through the sync proxy."""
        import pytest as _pt

        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            # Passing an invalid event kind (empty) should trigger validation
            with _pt.raises(Exception):
                sdk.audit.log(None)  # type: ignore[arg-type]


class TestSyncModuleProxyPassthrough:
    """Verify that non-coroutine methods pass through without wrapping."""

    def test_register_is_direct_call(self) -> None:
        with GovernanceSDKSync(**SDK_KWARGS) as sdk:
            # register is sync on the underlying module — should work
            policy = ScopePolicy(
                agent_id="direct-test",
                allowed_tools=frozenset({"x"}),
            )
            sdk.scope.register(policy)
            # Allowed tool: no exception raised
            sdk.scope.check("direct-test", tool="x")
