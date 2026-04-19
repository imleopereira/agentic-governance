"""Tests for the Microsoft Agent Framework (AGT) adapter.

AGT is NOT imported — the adapter duck-types against ``ChatAgent.run`` and
accepts AGT trace events as plain dicts. These tests verify both the
telemetry-ingestion path (``AGTBridge.consume``) and the pre-execution
enforcement wrap (``wrap_agt_agent``).
"""
from __future__ import annotations

import secrets
from typing import Any

import pytest
import pytest_asyncio

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.cost.module import CostModule
from codeatelier_governance.integrations.agt_wrap import (
    AGTBridge,
    AGTEventShapeError,
    ScopeViolationError,
    wrap_agt_agent,
)
from codeatelier_governance.scope.errors import ScopeViolation
from codeatelier_governance.scope.models import ScopePolicy
from codeatelier_governance.scope.module import ScopeModule


class FakeSDK:
    """Minimal SDK stand-in — same shape as other integration tests use."""

    def __init__(
        self,
        audit: AuditModule,
        scope: ScopeModule | None = None,
        cost: CostModule | None = None,
    ) -> None:
        self.audit = audit
        if scope is not None:
            self.scope = scope
        if cost is not None:
            self.cost = cost


@pytest_asyncio.fixture
async def store() -> InMemoryAuditStore:
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def audit(store: InMemoryAuditStore) -> AuditModule:
    writer = BatchingWriter(
        primary=store, batch_size=1, flush_interval_s=0.01, buffer_max=1000
    )
    module = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    await module.start()
    yield module  # type: ignore[misc]
    await module.close()


@pytest_asyncio.fixture
async def sdk_no_scope(audit: AuditModule) -> FakeSDK:
    """SDK with audit only — scope-unregistered fallthrough path."""
    return FakeSDK(audit=audit)


@pytest_asyncio.fixture
async def sdk_with_scope(audit: AuditModule) -> FakeSDK:
    """SDK with a scope policy for 'support-v1' allowing only 'read_ticket'."""
    scope = ScopeModule(audit)
    scope.register(
        ScopePolicy(
            agent_id="support-v1",
            allowed_tools=frozenset({"read_ticket"}),
        )
    )
    cost = CostModule(audit)
    return FakeSDK(audit=audit, scope=scope, cost=cost)


async def _flush_and_fetch(audit: AuditModule, store: InMemoryAuditStore) -> list[Any]:
    """Flush the batching writer then return all stored events."""
    await audit._writer.flush()
    return list(store._events.values())


# ---------------------------------------------------------------------------
# AGTBridge.consume — 3 known AGT shapes + unknown + malformed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_tool_call_event_logs_audit(
    sdk_no_scope: FakeSDK, audit: AuditModule, store: InMemoryAuditStore
) -> None:
    bridge = AGTBridge(sdk=sdk_no_scope, agent_id="support-v1")  # type: ignore[arg-type]
    ok = await bridge.consume(
        {
            "kind": "tool_call",
            "tool_name": "read_ticket",
            "span_id": "s-1",
            "duration_ms": 12,
        }
    )
    assert ok is True
    events = await _flush_and_fetch(audit, store)
    assert len(events) == 1
    assert events[0].kind == "tool.call"
    assert events[0].metadata["tool"] == "read_ticket"
    assert events[0].metadata["source"] == "microsoft_agent_framework"


@pytest.mark.asyncio
async def test_consume_llm_call_event_preserves_token_usage(
    sdk_no_scope: FakeSDK, audit: AuditModule, store: InMemoryAuditStore
) -> None:
    bridge = AGTBridge(sdk=sdk_no_scope, agent_id="support-v1")  # type: ignore[arg-type]
    ok = await bridge.consume(
        {
            "kind": "llm_call",
            "model": "gpt-4o-mini",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 42,
                "total_tokens": 142,
            },
        }
    )
    assert ok is True
    events = await _flush_and_fetch(audit, store)
    assert len(events) == 1
    assert events[0].kind == "llm.call"
    assert events[0].model == "gpt-4o-mini"
    assert events[0].metadata["token_usage"]["total_tokens"] == 142


@pytest.mark.asyncio
async def test_consume_hitl_approval_granted(
    sdk_no_scope: FakeSDK, audit: AuditModule, store: InMemoryAuditStore
) -> None:
    bridge = AGTBridge(sdk=sdk_no_scope, agent_id="support-v1")  # type: ignore[arg-type]
    ok = await bridge.consume(
        {
            "kind": "hitl_approval",
            "outcome": "granted",
            "approver": "diana@acme.test",
            "reason": "manual override per RCA",
        }
    )
    assert ok is True
    events = await _flush_and_fetch(audit, store)
    assert len(events) == 1
    assert events[0].kind == "approval.granted"
    assert events[0].metadata["approver"] == "diana@acme.test"


@pytest.mark.asyncio
async def test_consume_unknown_kind_is_skipped_not_raised(
    sdk_no_scope: FakeSDK, audit: AuditModule, store: InMemoryAuditStore
) -> None:
    bridge = AGTBridge(sdk=sdk_no_scope, agent_id="support-v1")  # type: ignore[arg-type]
    ok = await bridge.consume({"kind": "mcp_resource_list"})
    assert ok is False  # unknown kinds do NOT raise — telemetry keeps flowing
    events = await _flush_and_fetch(audit, store)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_consume_malformed_non_dict_raises(
    sdk_no_scope: FakeSDK,
) -> None:
    bridge = AGTBridge(sdk=sdk_no_scope, agent_id="support-v1")  # type: ignore[arg-type]
    with pytest.raises(AGTEventShapeError):
        await bridge.consume("not-a-dict")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_consume_missing_kind_raises(
    sdk_no_scope: FakeSDK,
) -> None:
    bridge = AGTBridge(sdk=sdk_no_scope, agent_id="support-v1")  # type: ignore[arg-type]
    with pytest.raises(AGTEventShapeError):
        await bridge.consume({"span_id": "s-1"})


@pytest.mark.asyncio
async def test_consume_preserves_trace_correlation_fields(
    sdk_no_scope: FakeSDK, audit: AuditModule, store: InMemoryAuditStore
) -> None:
    """Trace/span IDs from AGT MUST survive into audit metadata for correlation."""
    bridge = AGTBridge(sdk=sdk_no_scope, agent_id="support-v1")  # type: ignore[arg-type]
    await bridge.consume(
        {
            "kind": "tool_call",
            "tool_name": "read_ticket",
            "trace_id": "trace-abc",
            "span_id": "span-123",
            "parent_span_id": "span-000",
        }
    )
    events = await _flush_and_fetch(audit, store)
    assert events[0].metadata["trace_id"] == "trace-abc"
    assert events[0].metadata["span_id"] == "span-123"
    assert events[0].metadata["parent_span_id"] == "span-000"


def test_bridge_rejects_empty_agent_id(sdk_no_scope: FakeSDK) -> None:
    with pytest.raises(ValueError):
        AGTBridge(sdk=sdk_no_scope, agent_id="")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# wrap_agt_agent — pre-execution enforcement
# ---------------------------------------------------------------------------


class FakeAGTAgent:
    """Duck-typed AGT ChatAgent stand-in with an async run() method."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def run(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        return "agent-response"


@pytest.mark.asyncio
async def test_wrap_agt_agent_happy_path_runs_and_audits(
    sdk_with_scope: FakeSDK, audit: AuditModule, store: InMemoryAuditStore
) -> None:
    agent = FakeAGTAgent()
    wrapped = wrap_agt_agent(
        agent, sdk_with_scope, agent_id="support-v1"  # type: ignore[arg-type]
    )
    result = await wrapped.run(tool="read_ticket")
    assert result == "agent-response"
    assert len(agent.calls) == 1
    events = await _flush_and_fetch(audit, store)
    assert any(e.kind == "agt.run" for e in events)


@pytest.mark.asyncio
async def test_wrap_agt_agent_disallowed_tool_raises_scope_violation(
    sdk_with_scope: FakeSDK, audit: AuditModule, store: InMemoryAuditStore
) -> None:
    """PRD F4 acceptance: disallowed tool → ScopeViolation + deny audit event."""
    agent = FakeAGTAgent()
    wrapped = wrap_agt_agent(
        agent, sdk_with_scope, agent_id="support-v1"  # type: ignore[arg-type]
    )
    with pytest.raises(ScopeViolation):
        await wrapped.run(tool="delete_ticket")
    assert len(agent.calls) == 0  # original never ran — fail CLOSED
    events = await _flush_and_fetch(audit, store)
    assert any(e.kind == "scope.violation" for e in events)


def test_scope_violation_error_is_alias_of_scope_violation() -> None:
    """ScopeViolationError re-export MUST be the same class as ScopeViolation."""
    assert ScopeViolationError is ScopeViolation


@pytest.mark.asyncio
async def test_wrap_agt_agent_idempotent(
    sdk_with_scope: FakeSDK,
) -> None:
    """Double-wrapping a single agent is a WARN no-op, not a double-patch."""
    agent = FakeAGTAgent()
    wrap_agt_agent(agent, sdk_with_scope, agent_id="support-v1")  # type: ignore[arg-type]
    first_run = agent.run
    wrap_agt_agent(agent, sdk_with_scope, agent_id="support-v1")  # type: ignore[arg-type]
    # run reference unchanged on second call
    assert agent.run is first_run


def test_wrap_agt_agent_rejects_non_run_objects(
    sdk_with_scope: FakeSDK,
) -> None:
    with pytest.raises(ValueError):
        wrap_agt_agent(
            object(), sdk_with_scope, agent_id="support-v1"  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_wrap_agt_agent_propagates_agent_errors_and_audits(
    sdk_with_scope: FakeSDK, audit: AuditModule, store: InMemoryAuditStore
) -> None:
    class BrokenAgent:
        async def run(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("downstream AGT explosion")

    agent = BrokenAgent()
    wrapped = wrap_agt_agent(
        agent, sdk_with_scope, agent_id="support-v1"  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError):
        await wrapped.run(tool="read_ticket")
    events = await _flush_and_fetch(audit, store)
    assert any(e.kind == "agt.error" for e in events)


@pytest.mark.asyncio
async def test_audit_failure_does_not_break_consume(
    sdk_no_scope: FakeSDK,
) -> None:
    """Audit.log raising MUST NOT propagate out of consume (invariant #1)."""
    bridge = AGTBridge(sdk=sdk_no_scope, agent_id="support-v1")  # type: ignore[arg-type]

    async def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("audit DB unreachable")

    class _BrokenAudit:
        log = _raise

    sdk_no_scope.audit = _BrokenAudit()  # type: ignore[assignment]
    ok = await bridge.consume({"kind": "tool_call", "tool_name": "read_ticket"})
    # Host pipeline keeps flowing — we reported True because we attempted,
    # and the error was swallowed + logged.
    assert ok is True
