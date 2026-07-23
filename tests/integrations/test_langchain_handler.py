"""Tests for the LangChain GovernanceCallbackHandler.

All LangChain types are mocked; langchain-core is NOT required for these tests.
"""
from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from codeatelier_governance.audit import AuditModule, BatchingWriter, InMemoryAuditStore
from codeatelier_governance.cost.module import CostModule
from codeatelier_governance.scope.models import ScopePolicy
from codeatelier_governance.scope.module import ScopeModule
from codeatelier_governance.integrations.langchain_handler import GovernanceCallbackHandler


class FakeSDK:
    """Minimal SDK stand-in for testing."""

    def __init__(self, audit: AuditModule, scope: ScopeModule, cost: CostModule) -> None:
        self.audit = audit
        self.scope = scope
        self.cost = cost


@pytest_asyncio.fixture
async def store() -> InMemoryAuditStore:
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def secret_bytes() -> bytes:
    return secrets.token_bytes(32)


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
    scope.register(ScopePolicy(agent_id="test-agent", allowed_tools=frozenset({"read_file"})))
    cost = CostModule(audit)
    return FakeSDK(audit=audit, scope=scope, cost=cost)


@pytest_asyncio.fixture
async def handler(sdk: FakeSDK) -> GovernanceCallbackHandler:
    return GovernanceCallbackHandler(sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]


# -- Happy path: async callbacks -----------------------------------------------


@pytest.mark.asyncio
async def test_aon_llm_start_logs_audit_event(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    await handler.aon_llm_start(
        serialized={"name": "gpt-4"},
        prompts=["Hello"],
    )
    count = await store.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_aon_llm_end_logs_audit_event(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    response = MagicMock()
    response.llm_output = {"token_usage": {"total_tokens": 100}}
    await handler.aon_llm_end(response=response)
    count = await store.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_aon_llm_error_logs_audit_event(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    await handler.aon_llm_error(error=ValueError("boom"))
    count = await store.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_aon_tool_start_logs_audit_event(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    await handler.aon_tool_start(
        serialized={"name": "read_file"},
        input_str="test input",
    )
    count = await store.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_aon_tool_end_logs_audit_event(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    await handler.aon_tool_end(output="result")
    count = await store.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_aon_tool_error_logs_audit_event(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    await handler.aon_tool_error(error=RuntimeError("fail"))
    count = await store.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_aon_chain_start_logs_audit_event(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    await handler.aon_chain_start(
        serialized={"name": "my_chain"},
        inputs={"question": "test"},
    )
    count = await store.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_aon_chain_end_logs_audit_event(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    await handler.aon_chain_end(outputs={"answer": "42"})
    count = await store.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_aon_chain_error_logs_audit_event(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    await handler.aon_chain_error(error=ValueError("chain fail"))
    count = await store.count()
    assert count >= 1


# -- Non-breaking guarantee ----------------------------------------------------


@pytest.mark.asyncio
async def test_handler_does_not_raise_on_internal_failure(
    sdk: FakeSDK,
) -> None:
    """Even if audit.log raises internally, the handler swallows the error."""
    handler = GovernanceCallbackHandler(sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    # Monkey-patch audit.log to raise
    original_log = sdk.audit.log
    sdk.audit.log = AsyncMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]

    # None of these should raise
    await handler.aon_llm_start(serialized={"name": "gpt-4"}, prompts=["hi"])
    await handler.aon_llm_end(response=MagicMock(llm_output=None))
    await handler.aon_llm_error(error=ValueError("x"))
    await handler.aon_tool_start(serialized={"name": "t"}, input_str="")
    await handler.aon_tool_end(output="")
    await handler.aon_tool_error(error=ValueError("x"))
    await handler.aon_chain_start(serialized={"name": "c"}, inputs={})
    await handler.aon_chain_end(outputs={})
    await handler.aon_chain_error(error=ValueError("x"))

    # Restore
    sdk.audit.log = original_log  # type: ignore[method-assign]


# -- Scope violation: logged but not raised ------------------------------------


@pytest.mark.asyncio
async def test_scope_violation_logged_not_raised(
    handler: GovernanceCallbackHandler, store: InMemoryAuditStore
) -> None:
    """A tool not in the scope whitelist should be logged but not raised."""
    # "dangerous_tool" is not in the allowed_tools for test-agent
    await handler.aon_tool_start(
        serialized={"name": "dangerous_tool"},
        input_str="test",
    )
    # Should not raise; handler continues.
    # Check that a scope.violation event was logged.
    events = []
    for eid in store._order:
        ev = store._events.get(eid)
        if ev and ev.kind == "scope.violation":
            events.append(ev)
    assert len(events) >= 1


# -- SDK-4: enforce=True raises ScopeViolation --------------------------------


@pytest.mark.asyncio
async def test_enforce_true_raises_scope_violation(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """With enforce=True, a disallowed tool call should raise ScopeViolation."""
    from codeatelier_governance.scope.errors import ScopeViolation

    handler = GovernanceCallbackHandler(sdk=sdk, agent_id="test-agent", enforce=True)  # type: ignore[arg-type]

    with pytest.raises(ScopeViolation):
        await handler.aon_tool_start(
            serialized={"name": "dangerous_tool"},
            input_str="test",
        )

    # The violation should still be audit-logged
    events = []
    for eid in store._order:
        ev = store._events.get(eid)
        if ev and ev.kind == "scope.violation":
            events.append(ev)
    assert len(events) >= 1


@pytest.mark.asyncio
async def test_enforce_false_does_not_raise(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """With enforce=False (default), a disallowed tool call logs but does not raise."""
    handler = GovernanceCallbackHandler(sdk=sdk, agent_id="test-agent", enforce=False)  # type: ignore[arg-type]

    # Should NOT raise
    await handler.aon_tool_start(
        serialized={"name": "dangerous_tool"},
        input_str="test",
    )

    events = []
    for eid in store._order:
        ev = store._events.get(eid)
        if ev and ev.kind == "scope.violation":
            events.append(ev)
    assert len(events) >= 1


# -- Operator halt (kill switch) + default-deny enforcement -------------------


async def _wire_halted_agent(sdk: FakeSDK, agent_id: str) -> None:
    """Wire an in-memory presence module into scope and halt the agent."""
    from codeatelier_governance.presence import PresenceModule

    presence = PresenceModule()
    await presence.heartbeat(agent_id)
    await presence.halt(agent_id, halted_by="operator", reason="incident")
    await presence.force_refresh_halted_cache()
    sdk.scope.set_presence_module(presence)


@pytest.mark.asyncio
async def test_operator_halt_raises_even_without_enforce(sdk: FakeSDK) -> None:
    """An operator halt is a kill switch: it stops the tool even with enforce=False."""
    from codeatelier_governance.presence import AgentHaltedError

    await _wire_halted_agent(sdk, "test-agent")
    handler = GovernanceCallbackHandler(  # type: ignore[arg-type]
        sdk=sdk, agent_id="test-agent", enforce=False
    )

    # "read_file" is an ALLOWED tool, so only the halt can stop it.
    with pytest.raises(AgentHaltedError):
        await handler.aon_tool_start(serialized={"name": "read_file"}, input_str="x")


@pytest.mark.asyncio
async def test_operator_halt_raises_with_enforce(sdk: FakeSDK) -> None:
    """The halt also raises under enforce=True via the async callback."""
    from codeatelier_governance.presence import AgentHaltedError

    await _wire_halted_agent(sdk, "test-agent")
    handler = GovernanceCallbackHandler(  # type: ignore[arg-type]
        sdk=sdk, agent_id="test-agent", enforce=True
    )

    with pytest.raises(AgentHaltedError):
        await handler.aon_tool_start(serialized={"name": "read_file"}, input_str="x")


@pytest.mark.asyncio
async def test_default_deny_raises_under_enforce(sdk: FakeSDK) -> None:
    """An unregistered agent's tool is default-denied under enforce=True."""
    from codeatelier_governance.scope.errors import PolicyNotRegistered

    handler = GovernanceCallbackHandler(  # type: ignore[arg-type]
        sdk=sdk, agent_id="no-policy-agent", enforce=True
    )

    with pytest.raises(PolicyNotRegistered):
        await handler.aon_tool_start(serialized={"name": "read_file"}, input_str="x")


@pytest.mark.asyncio
async def test_default_deny_observed_without_enforce(sdk: FakeSDK) -> None:
    """Without enforce, an unregistered agent's tool is logged but not blocked."""
    handler = GovernanceCallbackHandler(  # type: ignore[arg-type]
        sdk=sdk, agent_id="no-policy-agent", enforce=False
    )

    # Must NOT raise.
    await handler.aon_tool_start(serialized={"name": "read_file"}, input_str="x")
