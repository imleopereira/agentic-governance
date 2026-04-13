"""End-to-end "5 lines to enforcement" test.

This is the test that goes in the README. It proves the pitch:
init SDK modules, register scope, check tool, log event, verify chain.
All in one test, under 10 lines of test body.
"""
from __future__ import annotations

import secrets
from typing import Any

import pytest
import structlog.testing

from codeatelier_governance.audit import (
    AuditEvent,
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.scope import ScopeModule, ScopePolicy, ScopeViolation


@pytest.mark.asyncio
async def test_five_lines_to_enforcement() -> None:
    """Init SDK, register scope, check tool, log event, verify chain."""
    store = InMemoryAuditStore()
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=BatchingWriter(primary=store, batch_size=1, flush_interval_s=0.01))
    await audit.start()
    scope = ScopeModule(audit)
    scope.register(ScopePolicy(agent_id="bot", allowed_tools=frozenset({"read"})))
    with pytest.raises(ScopeViolation):
        await scope.check(agent_id="bot", tool="delete")       # blocked
    record = await audit.log(AuditEvent(agent_id="bot", kind="tool.call"))
    await audit._writer.flush()
    chain = await audit.trace_session_chain(record.session_id)  # verified
    assert len(chain) >= 1
    await audit.close()


# ---------------------------------------------------------------------------
# Item 2: SDK.start() warning when no wrappers registered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_start_warns_when_no_wrappers_registered() -> None:
    """GovernanceSDK.start() emits no_wrappers_registered warning with no wrappers."""
    from codeatelier_governance.sdk import GovernanceSDK

    sdk = GovernanceSDK(database_url=None, api_key="test-key")

    with structlog.testing.capture_logs() as cap_logs:
        await sdk.start()
        await sdk.close()

    warning_events = [
        e for e in cap_logs
        if e.get("event") == "no_wrappers_registered"
    ]
    assert len(warning_events) >= 1, "Expected no_wrappers_registered warning at start()"


@pytest.mark.asyncio
async def test_sdk_start_no_warning_when_wrapper_registered() -> None:
    """GovernanceSDK.start() does NOT emit warning when wrap_anthropic() already called."""
    from codeatelier_governance.sdk import GovernanceSDK
    from codeatelier_governance.integrations.anthropic_wrap import wrap_anthropic

    sdk = GovernanceSDK(database_url=None, api_key="test-key")

    class FakeAsyncMsg:
        async def create(self, **kwargs: Any) -> Any:
            return None

    class FakeAsyncClientInner:
        messages = FakeAsyncMsg()

    client = FakeAsyncClientInner()
    wrap_anthropic(client, sdk=sdk, agent_id="pre-start-agent")  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap_logs:
        await sdk.start()
        await sdk.close()

    warning_events = [
        e for e in cap_logs
        if e.get("event") == "no_wrappers_registered"
    ]
    assert len(warning_events) == 0, "Must NOT warn when a wrapper is already registered"


@pytest.mark.asyncio
async def test_sdk_warn_on_no_wrappers_false_suppresses_warning() -> None:
    """warn_on_no_wrappers=False suppresses the no_wrappers_registered warning."""
    from codeatelier_governance.sdk import GovernanceSDK

    sdk = GovernanceSDK(database_url=None, api_key="test-key", warn_on_no_wrappers=False)

    with structlog.testing.capture_logs() as cap_logs:
        await sdk.start()
        await sdk.close()

    warning_events = [
        e for e in cap_logs
        if e.get("event") == "no_wrappers_registered"
    ]
    assert len(warning_events) == 0, "warn_on_no_wrappers=False must suppress the warning"


@pytest.mark.asyncio
async def test_sdk_wrap_after_start_emits_enforcement_active_log() -> None:
    """Calling wrap_anthropic() after sdk.start() emits enforcement_active info log."""
    from codeatelier_governance.sdk import GovernanceSDK
    from codeatelier_governance.integrations.anthropic_wrap import wrap_anthropic

    sdk = GovernanceSDK(database_url=None, api_key="test-key", warn_on_no_wrappers=False)
    await sdk.start()

    class FakeAsyncMsg:
        async def create(self, **kwargs: Any) -> Any:
            return None

    class FakeAsyncClientInner:
        messages = FakeAsyncMsg()

    client = FakeAsyncClientInner()

    with structlog.testing.capture_logs() as cap_logs:
        wrap_anthropic(client, sdk=sdk, agent_id="post-start-agent")  # type: ignore[arg-type]

    await sdk.close()

    info_events = [
        e for e in cap_logs
        if e.get("event") == "governance.anthropic_wrap.enforcement_active"
    ]
    assert len(info_events) >= 1, "Expected enforcement_active info when wrapping after start()"


# ---------------------------------------------------------------------------
# enable_loop=False / enable_presence=False config flags
# ---------------------------------------------------------------------------


def test_enable_loop_false_removes_loop_attribute() -> None:
    """GovernanceSDK(enable_loop=False) must not construct sdk.loop."""
    from codeatelier_governance.sdk import GovernanceSDK

    sdk = GovernanceSDK(api_key="test-key", enable_loop=False)
    assert not hasattr(sdk, "loop"), (
        "sdk.loop must not exist when enable_loop=False — "
        "callers must get AttributeError, not a silent no-op"
    )


def test_enable_presence_false_removes_presence_attribute() -> None:
    """GovernanceSDK(enable_presence=False) must not construct sdk.presence."""
    from codeatelier_governance.sdk import GovernanceSDK

    sdk = GovernanceSDK(api_key="test-key", enable_presence=False)
    assert not hasattr(sdk, "presence"), (
        "sdk.presence must not exist when enable_presence=False"
    )


def test_enable_loop_true_constructs_loop_attribute() -> None:
    """GovernanceSDK default must construct sdk.loop (enable_loop defaults to True)."""
    from codeatelier_governance.sdk import GovernanceSDK

    sdk = GovernanceSDK(api_key="test-key")
    assert hasattr(sdk, "loop"), "sdk.loop must exist by default"


# ---------------------------------------------------------------------------
# True e2e: GovernanceSDK + wrap_anthropic → budget decremented + audit logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_enforcement_wrap_anthropic_budget_and_audit() -> None:
    """Full enforcement flow using the real GovernanceSDK entry point.

    Proves the pitch end-to-end:
      1. Instantiate GovernanceSDK (the actual public API, no manual module construction)
      2. Register a budget policy
      3. Wrap an Anthropic client via wrap_anthropic()
      4. Make a call — scope gate passes, budget gate passes, LLM call executes
      5. Assert budget was decremented from the session balance
      6. Assert an audit event was written with the correct agent_id
    """
    from codeatelier_governance.cost.models import BudgetPolicy
    from codeatelier_governance.integrations.anthropic_wrap import wrap_anthropic
    from codeatelier_governance.sdk import GovernanceSDK
    from uuid import uuid4

    class FakeUsage:
        input_tokens = 100
        output_tokens = 50

    class FakeResponse:
        model = "claude-sonnet-4-6"
        usage = FakeUsage()

    class FakeMessages:
        async def create(self, **kwargs: Any) -> FakeResponse:
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    sdk = GovernanceSDK(api_key="test-key", warn_on_no_wrappers=False)
    async with sdk:
        sdk.cost.register(BudgetPolicy(agent_id="e2e-agent", per_session_tokens=10_000))

        session_id = uuid4()
        client = FakeClient()
        wrap_anthropic(client, sdk=sdk, agent_id="e2e-agent", session_id=session_id)  # type: ignore[arg-type]

        await client.messages.create(model="claude-sonnet-4-6", max_tokens=200)
        await sdk.audit._writer.flush()

        # Budget decremented: 150 actual tokens (100 + 50)
        snap = await sdk.cost.snapshot("e2e-agent", session_id)
        assert snap.session_tokens_used == 150, (
            f"Expected 150 tokens tracked, got {snap.session_tokens_used}"
        )

        # Audit event written for this agent
        from codeatelier_governance.audit.store import InMemoryAuditStore
        assert isinstance(sdk.audit._store, InMemoryAuditStore)
        events = list(sdk.audit._store._events.values())
        agent_events = [e for e in events if e.agent_id == "e2e-agent"]
        assert len(agent_events) >= 1, (
            "Expected at least one audit event for e2e-agent"
        )
