"""Tests for the Anthropic wrap_anthropic adapter.

All Anthropic types are mocked; the anthropic package is NOT required for these tests.
"""
from __future__ import annotations

import secrets
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from codeatelier_governance.audit import AuditModule, BatchingWriter, InMemoryAuditStore
from codeatelier_governance.cost.errors import BudgetExceeded
from codeatelier_governance.cost.models import BudgetPolicy
from codeatelier_governance.cost.module import CostModule
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
