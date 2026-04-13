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
