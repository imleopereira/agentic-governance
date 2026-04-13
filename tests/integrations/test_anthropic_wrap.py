"""Tests for the Anthropic wrap_anthropic adapter.

All Anthropic types are mocked; the anthropic package is NOT required for these tests.
"""
from __future__ import annotations

import secrets
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from codeatelier_governance.audit import AuditModule, BatchingWriter, InMemoryAuditStore
from codeatelier_governance.cost.errors import BudgetExceeded
from codeatelier_governance.cost.models import BudgetPolicy
from codeatelier_governance.cost.module import CostModule
from codeatelier_governance.scope.errors import ScopeViolation
from codeatelier_governance.scope.models import ScopePolicy
from codeatelier_governance.scope.module import ScopeModule
from codeatelier_governance.integrations.anthropic_wrap import wrap_anthropic


class FakeSDK:
    """Minimal SDK stand-in for testing."""

    def __init__(self, audit: AuditModule, scope: ScopeModule, cost: CostModule) -> None:
        self.audit = audit
        self.scope = scope
        self.cost = cost


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(self, model: str = "claude-sonnet-4-6", usage: FakeUsage | None = None) -> None:
        self.model = model
        self.usage = usage


class FakeMessages:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    def create(self, **kwargs: Any) -> FakeResponse:
        return self._response


class FakeAsyncMessages:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    async def create(self, **kwargs: Any) -> FakeResponse:
        return self._response


class FakeSyncClient:
    def __init__(self, response: FakeResponse) -> None:
        self.messages = FakeMessages(response)


class FakeAsyncClient:
    def __init__(self, response: FakeResponse) -> None:
        self.messages = FakeAsyncMessages(response)


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
    cost = CostModule(audit)
    return FakeSDK(audit=audit, scope=scope, cost=cost)


# -- Sync client tests ---------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_patches_create_method(
    sdk: FakeSDK,
) -> None:
    """wrap_anthropic should patch client.messages.create in place."""
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeSyncClient(response)

    original_create = client.messages.create
    wrapped = wrap_anthropic(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    assert wrapped is client
    assert client.messages.create is not original_create
    assert getattr(client, "_governance_wrapped", False) is True


@pytest.mark.asyncio
async def test_double_wrap_warns_and_returns(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Calling wrap_anthropic twice on the same client should be a no-op the second time."""
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    wrap_anthropic(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]
    assert getattr(client, "_governance_wrapped", False) is True

    # Wrap again -- should return immediately
    wrap_anthropic(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    # Call once -- should produce only 1 set of audit events (not doubled)
    await client.messages.create(model="claude-sonnet-4-6")

    count = await store.count()
    # Should have exactly llm.call + llm.result = 2 events, not 4
    assert count == 2


@pytest.mark.asyncio
async def test_sync_wrapper_raises_in_async_context(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_anthropic sync wrapper must raise RuntimeError inside event loop.

    The sync wrapper detects a running event loop and raises a clear error
    directing the developer to use the async API instead.
    """
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeSyncClient(response)

    wrapped = wrap_anthropic(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]
    assert wrapped is client

    with pytest.raises(RuntimeError, match="sync Anthropic wrapper called inside a running event loop"):
        wrapped.messages.create(model="claude-sonnet-4-6")


@pytest.mark.asyncio
async def test_cost_tracked_on_success(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Cost tracking should record tokens after a successful call."""
    response = FakeResponse(usage=FakeUsage(100, 200))
    client = FakeAsyncClient(response)

    wrap_anthropic(client, sdk=sdk, agent_id="cost-agent")  # type: ignore[arg-type]
    await client.messages.create(model="claude-sonnet-4-6")

    # Verify audit events include token usage (cost.track is observation-only
    # with in-memory store; we verify via audit events that contain token_usage).
    count = await store.count()
    assert count >= 2  # llm.call + llm.result at minimum

    # Verify the llm.result event has token_usage metadata by scanning stored events.
    all_events = list(store._events.values())
    result_events = [e for e in all_events if e.kind == "llm.result"]
    assert len(result_events) == 1
    assert result_events[0].metadata["token_usage"]["total_tokens"] == 300


@pytest.mark.asyncio
async def test_audit_event_emitted_on_error(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """On async LLM error, wrap_anthropic should log llm.error and re-raise."""
    response = FakeResponse()
    client = FakeAsyncClient(response)

    async def failing_create(**kwargs: Any) -> FakeResponse:
        raise RuntimeError("API error")

    client.messages.create = failing_create  # type: ignore[method-assign]

    wrapped = wrap_anthropic(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="API error"):
        await wrapped.messages.create(model="claude-sonnet-4-6")

    # Should have logged llm.call + llm.error
    count = await store.count()
    assert count >= 2


@pytest.mark.asyncio
async def test_budget_check_runs_before_call(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """cost.check_or_raise BudgetExceeded should propagate to the caller."""
    # Register a daily budget and pre-fill usage to exceed it
    sdk.cost.register(BudgetPolicy(agent_id="tight-agent", per_agent_usd_daily=0.001))
    sid = uuid4()
    await sdk.cost.track("tight-agent", sid, tokens=0, usd=1.0)

    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)
    wrapped = wrap_anthropic(client, sdk=sdk, agent_id="tight-agent")  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded):
        await wrapped.messages.create(model="claude-sonnet-4-6")


# -- Async client tests --------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_async_client_audit_events(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_anthropic on an async client should log llm.call and llm.result."""
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    wrapped = wrap_anthropic(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]
    assert wrapped is client

    result = await wrapped.messages.create(model="claude-sonnet-4-6")
    assert result is response

    count = await store.count()
    assert count >= 2


@pytest.mark.asyncio
async def test_wrap_async_client_error_audit(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """On async LLM error, should log llm.error and re-raise."""
    response = FakeResponse()
    client = FakeAsyncClient(response)

    async def failing_create(**kwargs: Any) -> FakeResponse:
        raise RuntimeError("Async API error")

    client.messages.create = failing_create  # type: ignore[method-assign]

    wrapped = wrap_anthropic(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="Async API error"):
        await wrapped.messages.create(model="claude-sonnet-4-6")

    count = await store.count()
    assert count >= 2


# -- Fix 1: Session ID stability -----------------------------------------------


@pytest.mark.asyncio
async def test_session_id_shared_across_calls(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Two consecutive calls through a wrapped client must share the same session_id."""
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    wrapped = wrap_anthropic(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    await wrapped.messages.create(model="claude-sonnet-4-6")
    await wrapped.messages.create(model="claude-sonnet-4-6")

    all_events = list(store._events.values())
    session_ids = {e.session_id for e in all_events}
    # All events should share the same session_id
    assert len(session_ids) == 1


@pytest.mark.asyncio
async def test_explicit_session_id_is_used(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """An explicit session_id passed to wrap_anthropic must be used."""
    from uuid import uuid4 as _uuid4

    explicit_sid = _uuid4()
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    wrapped = wrap_anthropic(client, sdk=sdk, agent_id="test-agent", session_id=explicit_sid)  # type: ignore[arg-type]
    assert getattr(wrapped, "_governance_session_id") == explicit_sid

    await wrapped.messages.create(model="claude-sonnet-4-6")

    all_events = list(store._events.values())
    for event in all_events:
        assert event.session_id == explicit_sid


@pytest.mark.asyncio
async def test_model_field_set_on_audit_events(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """AuditEvent.model should be set as a first-class field, not just in metadata."""
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    wrap_anthropic(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]
    await client.messages.create(model="claude-sonnet-4-6")

    all_events = list(store._events.values())
    llm_events = [e for e in all_events if e.kind in ("llm.call", "llm.result")]
    assert len(llm_events) >= 2
    for event in llm_events:
        assert event.model is not None
        assert "claude" in event.model


# -- Gap #1: Streaming response detection (Anthropic) --------------------------


class TestIsStreamingResponseAnthropic:
    """_is_streaming_response should detect Anthropic streaming types by class name."""

    def test_message_stream_detected(self) -> None:
        from codeatelier_governance.integrations.anthropic_wrap import _is_streaming_response

        class MessageStream:
            pass

        assert _is_streaming_response(MessageStream()) is True

    def test_async_message_stream_detected(self) -> None:
        from codeatelier_governance.integrations.anthropic_wrap import _is_streaming_response

        class AsyncMessageStream:
            pass

        assert _is_streaming_response(AsyncMessageStream()) is True

    def test_stream_type_detected(self) -> None:
        from codeatelier_governance.integrations.anthropic_wrap import _is_streaming_response

        class Stream:
            pass

        assert _is_streaming_response(Stream()) is True

    def test_async_stream_type_detected(self) -> None:
        from codeatelier_governance.integrations.anthropic_wrap import _is_streaming_response

        class AsyncStream:
            pass

        assert _is_streaming_response(AsyncStream()) is True

    def test_normal_response_not_detected(self) -> None:
        from codeatelier_governance.integrations.anthropic_wrap import _is_streaming_response

        assert _is_streaming_response(FakeResponse()) is False

    def test_none_not_detected(self) -> None:
        from codeatelier_governance.integrations.anthropic_wrap import _is_streaming_response

        assert _is_streaming_response(None) is False


# ---------------------------------------------------------------------------
# Item 1: scope.check() integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_violation_blocks_llm_call(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_anthropic with scope policy denying the action raises ScopeViolation.

    The LLM client must never be called when scope check fails.
    """
    sdk.scope.register(
        ScopePolicy(agent_id="scope-agent", allowed_tools=frozenset({"read_file"}))
    )
    # messages.create sentinel is "messages.create" — not in allowed_tools
    call_count = 0

    class CountingMessages:
        async def create(self, **kwargs: Any) -> FakeResponse:
            nonlocal call_count
            call_count += 1
            return FakeResponse(usage=FakeUsage(10, 20))

    class CountingClient:
        messages = CountingMessages()

    client = CountingClient()
    wrap_anthropic(client, sdk=sdk, agent_id="scope-agent")  # type: ignore[arg-type]

    with pytest.raises(ScopeViolation):
        await client.messages.create(model="claude-sonnet-4-6")

    assert call_count == 0, "LLM client must not be called when scope check fails"


@pytest.mark.asyncio
async def test_no_scope_policy_passes_through(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_anthropic with no scope policy registered proceeds normally."""
    # No scope.register() call — open scope, pass-through
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    wrap_anthropic(client, sdk=sdk, agent_id="unregistered-agent")  # type: ignore[arg-type]

    result = await client.messages.create(model="claude-sonnet-4-6")
    assert result is response


@pytest.mark.asyncio
async def test_scope_check_fires_before_cost_check(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Scope violation must fire BEFORE cost check — cost mock never called."""
    sdk.scope.register(
        ScopePolicy(agent_id="order-agent", allowed_tools=frozenset({"read_file"}))
    )
    # Pre-fill budget to also exceed it — but scope should fire first
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="order-agent", per_session_usd=0.001))
    await sdk.cost.track("order-agent", sid, tokens=0, usd=999.0)

    cost_check_called = False
    original_check = sdk.cost.check_or_raise

    async def tracking_check(*args: Any, **kwargs: Any) -> None:
        nonlocal cost_check_called
        cost_check_called = True
        return await original_check(*args, **kwargs)

    sdk.cost.check_or_raise = tracking_check  # type: ignore[method-assign]

    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)
    wrap_anthropic(client, sdk=sdk, agent_id="order-agent", session_id=sid)  # type: ignore[arg-type]

    with pytest.raises(ScopeViolation):
        await client.messages.create(model="claude-sonnet-4-6")

    assert not cost_check_called, "cost.check_or_raise must not be called when scope denies"


@pytest.mark.asyncio
async def test_scope_allowed_tool_passes(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_anthropic with a policy that allows messages.create sentinel passes."""
    sdk.scope.register(
        ScopePolicy(
            agent_id="allowed-agent",
            allowed_tools=frozenset({"messages.create"}),
        )
    )
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)

    wrap_anthropic(client, sdk=sdk, agent_id="allowed-agent")  # type: ignore[arg-type]
    result = await client.messages.create(model="claude-sonnet-4-6", max_tokens=100)
    assert result is response


# ---------------------------------------------------------------------------
# Item 2: wrapper registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_anthropic_registers_on_sdk(sdk: FakeSDK) -> None:
    """wrap_anthropic should append itself to sdk._registered_wrappers."""
    # FakeSDK does not have _registered_wrappers — add it to simulate real SDK
    sdk._registered_wrappers = []  # type: ignore[attr-defined]
    sdk._started = False  # type: ignore[attr-defined]
    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)
    wrap_anthropic(client, sdk=sdk, agent_id="reg-agent")  # type: ignore[arg-type]
    assert "anthropic:reg-agent" in sdk._registered_wrappers  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Item 3: projected tokens budget gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projected_tokens_blocks_call_before_llm(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Budget gate with projected tokens: balance 800, limit 1000, max_tokens 300 → blocked."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="proj-agent", per_session_tokens=1000))
    await sdk.cost.track("proj-agent", sid, tokens=800)

    call_count = 0

    class CountingMessages:
        async def create(self, **kwargs: Any) -> FakeResponse:
            nonlocal call_count
            call_count += 1
            return FakeResponse(usage=FakeUsage(10, 20))

    class CountingClient:
        messages = CountingMessages()

    client = CountingClient()
    wrap_anthropic(client, sdk=sdk, agent_id="proj-agent", session_id=sid)  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded):
        await client.messages.create(model="claude-sonnet-4-6", max_tokens=300)

    assert call_count == 0, "LLM must not be called when projected usage would breach limit"


@pytest.mark.asyncio
async def test_projected_tokens_under_limit_proceeds(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Budget gate: balance 800, limit 1000, max_tokens 100 → call proceeds."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="proj-agent2", per_session_tokens=1000))
    await sdk.cost.track("proj-agent2", sid, tokens=800)

    response = FakeResponse(usage=FakeUsage(50, 50))
    client = FakeAsyncClient(response)
    wrap_anthropic(client, sdk=sdk, agent_id="proj-agent2", session_id=sid)  # type: ignore[arg-type]

    result = await client.messages.create(model="claude-sonnet-4-6", max_tokens=100)
    assert result is response


@pytest.mark.asyncio
async def test_no_max_tokens_emits_warning_and_proceeds(
    sdk: FakeSDK, store: InMemoryAuditStore, caplog: Any
) -> None:
    """No max_tokens and no default → warning emitted, call proceeds."""
    import logging
    import structlog.testing

    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)
    wrap_anthropic(client, sdk=sdk, agent_id="warn-agent")  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap_logs:
        result = await client.messages.create(model="claude-sonnet-4-6")  # no max_tokens

    warning_events = [
        e for e in cap_logs
        if e.get("event") == "governance.wrap.max_tokens_not_declared"
    ]
    assert len(warning_events) >= 1, "Expected max_tokens_not_declared warning"
    assert result is response


@pytest.mark.asyncio
async def test_default_max_tokens_used_when_not_declared(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """SDK default_max_tokens used as projection when call omits max_tokens."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="default-proj", per_session_tokens=1000))
    await sdk.cost.track("default-proj", sid, tokens=800)

    # Attach a config with default_max_tokens=500 (would project to 1300 > 1000)
    class FakeConfig:
        default_max_tokens = 500
        warn_on_no_wrappers = False

    sdk.config = FakeConfig()  # type: ignore[attr-defined]

    response = FakeResponse(usage=FakeUsage(10, 20))
    client = FakeAsyncClient(response)
    wrap_anthropic(client, sdk=sdk, agent_id="default-proj", session_id=sid)  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded):
        await client.messages.create(model="claude-sonnet-4-6")  # no explicit max_tokens


# ---------------------------------------------------------------------------
# Item 4: Streaming — no cost_tracked: False after this patch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_budget_gate_blocks_before_stream_opened(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Streaming call where max_tokens would breach limit → BudgetExceeded, stream never opened."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="stream-gate", per_session_tokens=1000))
    await sdk.cost.track("stream-gate", sid, tokens=800)

    stream_opened = False

    class FakeStream:
        def __init__(self) -> None:
            nonlocal stream_opened
            stream_opened = True

    class StreamingMessages:
        async def create(self, **kwargs: Any) -> FakeStream:
            return FakeStream()

    class StreamingClient:
        messages = StreamingMessages()

    client = StreamingClient()
    wrap_anthropic(client, sdk=sdk, agent_id="stream-gate", session_id=sid)  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded):
        await client.messages.create(model="claude-sonnet-4-6", stream=True, max_tokens=300)

    assert not stream_opened, "Stream must not be opened when budget gate fires"


@pytest.mark.asyncio
async def test_streaming_call_tracks_cost(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Streaming call that completes → track() called with projected tokens."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="stream-track", per_session_tokens=5000))

    class FakeStream:
        pass

    class StreamingMessages:
        async def create(self, **kwargs: Any) -> FakeStream:
            return FakeStream()

    class StreamingClient:
        messages = StreamingMessages()

    client = StreamingClient()
    wrap_anthropic(client, sdk=sdk, agent_id="stream-track", session_id=sid)  # type: ignore[arg-type]

    await client.messages.create(model="claude-sonnet-4-6", stream=True, max_tokens=200)

    # The wrapper should have tracked 200 projected tokens
    snap = await sdk.cost.snapshot("stream-track", sid)
    assert snap.session_tokens_used == 200


@pytest.mark.asyncio
async def test_streaming_audit_event_cost_tracked_true(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Streaming call with max_tokens → llm.result audit event has cost_tracked=True, not False."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="stream-audit", per_session_tokens=5000))

    class FakeStream:
        pass

    class StreamingMessages:
        async def create(self, **kwargs: Any) -> FakeStream:
            return FakeStream()

    class StreamingClient:
        messages = StreamingMessages()

    client = StreamingClient()
    wrap_anthropic(client, sdk=sdk, agent_id="stream-audit", session_id=sid)  # type: ignore[arg-type]

    await client.messages.create(model="claude-sonnet-4-6", stream=True, max_tokens=150)
    await sdk.audit._writer.flush()

    all_events = list(store._events.values())
    result_events = [e for e in all_events if e.kind == "llm.result"]
    assert len(result_events) >= 1

    for event in result_events:
        assert event.metadata.get("cost_tracked") is not False, (
            f"Streaming audit event must not have cost_tracked=False, got: {event.metadata}"
        )


@pytest.mark.asyncio
async def test_streaming_no_max_tokens_emits_warning(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Streaming with no max_tokens → structlog warning emitted."""
    import structlog.testing

    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="stream-warn", per_session_tokens=5000))

    class FakeStream:
        pass

    class StreamingMessages:
        async def create(self, **kwargs: Any) -> FakeStream:
            return FakeStream()

    class StreamingClient:
        messages = StreamingMessages()

    client = StreamingClient()
    wrap_anthropic(client, sdk=sdk, agent_id="stream-warn", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap_logs:
        await client.messages.create(model="claude-sonnet-4-6", stream=True)
        # No max_tokens passed

    warning_events = [
        e for e in cap_logs
        if e.get("event") in (
            "governance.wrap.max_tokens_not_declared",
            "governance.anthropic_wrap.streaming_no_usage",
        )
    ]
    assert len(warning_events) >= 1, "Expected a warning when no max_tokens for streaming"
