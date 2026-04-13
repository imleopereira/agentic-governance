"""Tests for the OpenAI wrap_openai adapter.

All OpenAI types are mocked; the openai package is NOT required for these tests.
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
from codeatelier_governance.scope.errors import ScopeViolation
from codeatelier_governance.scope.models import ScopePolicy
from codeatelier_governance.scope.module import ScopeModule
from codeatelier_governance.integrations.openai_wrap import wrap_openai


class FakeSDK:
    """Minimal SDK stand-in for testing."""

    def __init__(self, audit: AuditModule, scope: ScopeModule, cost: CostModule) -> None:
        self.audit = audit
        self.scope = scope
        self.cost = cost


class FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class FakeResponse:
    def __init__(self, usage: FakeUsage | None = None) -> None:
        self.usage = usage


class FakeCompletions:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    def create(self, **kwargs: Any) -> FakeResponse:
        return self._response


class FakeAsyncCompletions:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    async def create(self, **kwargs: Any) -> FakeResponse:
        return self._response


class FakeChat:
    def __init__(self, completions: Any) -> None:
        self.completions = completions


class FakeSyncClient:
    def __init__(self, response: FakeResponse) -> None:
        self.chat = FakeChat(FakeCompletions(response))


class FakeAsyncClient:
    def __init__(self, response: FakeResponse) -> None:
        self.chat = FakeChat(FakeAsyncCompletions(response))


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
async def test_wrap_sync_client_raises_in_async_context(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_openai on a sync client must raise RuntimeError inside an event loop.

    The sync wrapper detects a running event loop and raises a clear error
    directing the developer to use the async API instead. This prevents the
    old behavior of silently returning a coroutine object.
    """
    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeSyncClient(response)

    wrapped = wrap_openai(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]
    assert wrapped is client  # Same object returned

    with pytest.raises(RuntimeError, match="sync OpenAI wrapper called inside a running event loop"):
        wrapped.chat.completions.create(model="gpt-4o")


# -- Async client tests --------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_async_client_audit_events(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_openai on an async client should log llm.call and llm.result."""
    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)

    wrapped = wrap_openai(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]
    assert wrapped is client

    result = await wrapped.chat.completions.create(model="gpt-4o")
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

    client.chat.completions.create = failing_create  # type: ignore[method-assign]

    wrapped = wrap_openai(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="Async API error"):
        await wrapped.chat.completions.create(model="gpt-4o")

    count = await store.count()
    assert count >= 2


# -- Budget enforcement --------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_async_budget_exceeded_raises(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """cost.check_or_raise BudgetExceeded should propagate to the caller."""
    # Register a very tight budget
    sdk.cost.register(BudgetPolicy(agent_id="tight-agent", per_session_usd=0.001))
    # Pre-fill usage to exceed budget
    sid = uuid4()
    await sdk.cost.track("tight-agent", sid, tokens=0, usd=1.0)

    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)
    wrap_openai(client, sdk=sdk, agent_id="tight-agent")  # type: ignore[arg-type]

    # The check_or_raise runs with a new session_id each time in the wrapper,
    # but daily limits will catch it. Let's use a daily limit instead.
    sdk.cost.register(BudgetPolicy(agent_id="tight-agent2", per_agent_usd_daily=0.001))
    await sdk.cost.track("tight-agent2", sid, tokens=0, usd=1.0)

    client2 = FakeAsyncClient(response)
    wrapped2 = wrap_openai(client2, sdk=sdk, agent_id="tight-agent2")  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded):
        await wrapped2.chat.completions.create(model="gpt-4o")


# -- SDK-3: Double-wrap sentinel -----------------------------------------------


@pytest.mark.asyncio
async def test_double_wrap_returns_same_client(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Calling wrap_openai twice on the same client should be a no-op the second time."""
    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)

    wrap_openai(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]
    assert getattr(client, "_governance_wrapped", False) is True

    # Wrap again — should return immediately
    wrap_openai(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    # Call once — should produce only 1 set of audit events (not doubled)
    await client.chat.completions.create(model="gpt-4o")

    count = await store.count()
    # Should have exactly llm.call + llm.result = 2 events, not 4
    assert count == 2


# -- Fix 1: Session ID stability -----------------------------------------------


@pytest.mark.asyncio
async def test_session_id_shared_across_calls(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """Two consecutive calls through a wrapped client must share the same session_id."""
    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)

    wrapped = wrap_openai(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    await wrapped.chat.completions.create(model="gpt-4o")
    await wrapped.chat.completions.create(model="gpt-4o")

    all_events = list(store._events.values())
    session_ids = {e.session_id for e in all_events}
    # All events should share the same session_id
    assert len(session_ids) == 1


@pytest.mark.asyncio
async def test_explicit_session_id_is_used(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """An explicit session_id passed to wrap_openai must be used."""
    from uuid import uuid4 as _uuid4

    explicit_sid = _uuid4()
    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)

    wrapped = wrap_openai(client, sdk=sdk, agent_id="test-agent", session_id=explicit_sid)  # type: ignore[arg-type]
    assert getattr(wrapped, "_governance_session_id") == explicit_sid

    await wrapped.chat.completions.create(model="gpt-4o")

    all_events = list(store._events.values())
    for event in all_events:
        assert event.session_id == explicit_sid


@pytest.mark.asyncio
async def test_model_field_set_on_audit_events(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """AuditEvent.model should be set as a first-class field, not just in metadata."""
    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)

    wrap_openai(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]
    await client.chat.completions.create(model="gpt-4o")

    all_events = list(store._events.values())
    llm_events = [e for e in all_events if e.kind in ("llm.call", "llm.result")]
    assert len(llm_events) >= 2
    for event in llm_events:
        assert event.model == "gpt-4o"


# -- Gap #1: Streaming response detection -------------------------------------


class TestIsStreamingResponse:
    """_is_streaming_response should detect OpenAI streaming types by class name."""

    def test_stream_type_detected(self) -> None:
        from codeatelier_governance.integrations.openai_wrap import _is_streaming_response

        class Stream:
            pass

        assert _is_streaming_response(Stream()) is True

    def test_async_stream_type_detected(self) -> None:
        from codeatelier_governance.integrations.openai_wrap import _is_streaming_response

        class AsyncStream:
            pass

        assert _is_streaming_response(AsyncStream()) is True

    def test_normal_response_not_detected(self) -> None:
        from codeatelier_governance.integrations.openai_wrap import _is_streaming_response

        assert _is_streaming_response(FakeResponse()) is False

    def test_string_not_detected(self) -> None:
        from codeatelier_governance.integrations.openai_wrap import _is_streaming_response

        assert _is_streaming_response("Stream") is False

    def test_none_not_detected(self) -> None:
        from codeatelier_governance.integrations.openai_wrap import _is_streaming_response

        assert _is_streaming_response(None) is False


# ---------------------------------------------------------------------------
# Item 1: scope.check() integration for OpenAI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_scope_violation_blocks_llm_call(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_openai with scope policy denying the action raises ScopeViolation.

    The LLM client must never be called when scope check fails.
    """
    sdk.scope.register(
        ScopePolicy(agent_id="scope-agent-oai", allowed_tools=frozenset({"read_file"}))
    )
    call_count = 0

    class CountingCompletions:
        async def create(self, **kwargs: Any) -> FakeResponse:
            nonlocal call_count
            call_count += 1
            return FakeResponse(usage=FakeUsage(10, 20, 30))

    class CountingChat:
        completions = CountingCompletions()

    class CountingClient:
        chat = CountingChat()

    client = CountingClient()
    wrap_openai(client, sdk=sdk, agent_id="scope-agent-oai")  # type: ignore[arg-type]

    with pytest.raises(ScopeViolation):
        await client.chat.completions.create(model="gpt-4o")

    assert call_count == 0, "LLM client must not be called when scope check fails"


@pytest.mark.asyncio
async def test_openai_no_scope_policy_passes_through(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_openai with no scope policy registered proceeds normally."""
    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)

    wrap_openai(client, sdk=sdk, agent_id="unreg-oai")  # type: ignore[arg-type]

    result = await client.chat.completions.create(model="gpt-4o")
    assert result is response


@pytest.mark.asyncio
async def test_openai_scope_check_fires_before_cost_check(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """OpenAI scope violation fires BEFORE cost check."""
    sdk.scope.register(
        ScopePolicy(agent_id="order-agent-oai", allowed_tools=frozenset({"read_file"}))
    )
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="order-agent-oai", per_session_usd=0.001))
    await sdk.cost.track("order-agent-oai", sid, tokens=0, usd=999.0)

    cost_check_called = False
    original_check = sdk.cost.check_or_raise

    async def tracking_check(*args: Any, **kwargs: Any) -> None:
        nonlocal cost_check_called
        cost_check_called = True
        return await original_check(*args, **kwargs)

    sdk.cost.check_or_raise = tracking_check  # type: ignore[method-assign]

    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)
    wrap_openai(client, sdk=sdk, agent_id="order-agent-oai", session_id=sid)  # type: ignore[arg-type]

    with pytest.raises(ScopeViolation):
        await client.chat.completions.create(model="gpt-4o")

    assert not cost_check_called, "cost.check_or_raise must not be called when scope denies"


# ---------------------------------------------------------------------------
# Item 2: wrapper registry for OpenAI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_openai_registers_on_sdk(sdk: FakeSDK) -> None:
    """wrap_openai should append itself to sdk._registered_wrappers."""
    sdk._registered_wrappers = []  # type: ignore[attr-defined]
    sdk._started = False  # type: ignore[attr-defined]
    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)
    wrap_openai(client, sdk=sdk, agent_id="reg-oai")  # type: ignore[arg-type]
    assert "openai:reg-oai" in sdk._registered_wrappers  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Item 3: projected tokens for OpenAI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_projected_tokens_blocks_call(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """OpenAI budget gate: balance 800 tokens, limit 1000, max_tokens 300 → blocked."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="proj-oai", per_session_tokens=1000))
    await sdk.cost.track("proj-oai", sid, tokens=800)

    call_count = 0

    class CountingCompletions:
        async def create(self, **kwargs: Any) -> FakeResponse:
            nonlocal call_count
            call_count += 1
            return FakeResponse(usage=FakeUsage(10, 20, 30))

    class CountingChat:
        completions = CountingCompletions()

    class CountingClient:
        chat = CountingChat()

    client = CountingClient()
    wrap_openai(client, sdk=sdk, agent_id="proj-oai", session_id=sid)  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded):
        await client.chat.completions.create(model="gpt-4o", max_tokens=300)

    assert call_count == 0, "LLM must not be called when projected usage would breach limit"


@pytest.mark.asyncio
async def test_openai_projected_tokens_under_limit_proceeds(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """OpenAI budget gate: balance 800, limit 1000, max_tokens 100 → proceeds."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="proj-oai2", per_session_tokens=1000))
    await sdk.cost.track("proj-oai2", sid, tokens=800)

    response = FakeResponse(usage=FakeUsage(50, 50, 100))
    client = FakeAsyncClient(response)
    wrap_openai(client, sdk=sdk, agent_id="proj-oai2", session_id=sid)  # type: ignore[arg-type]

    result = await client.chat.completions.create(model="gpt-4o", max_tokens=100)
    assert result is response


@pytest.mark.asyncio
async def test_openai_no_max_tokens_emits_warning(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """OpenAI: no max_tokens and no default → warning emitted, call proceeds."""
    import structlog.testing

    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)
    wrap_openai(client, sdk=sdk, agent_id="warn-oai")  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap_logs:
        result = await client.chat.completions.create(model="gpt-4o")

    warning_events = [
        e for e in cap_logs
        if e.get("event") == "governance.wrap.max_tokens_not_declared"
    ]
    assert len(warning_events) >= 1, "Expected max_tokens_not_declared warning"
    assert result is response


@pytest.mark.asyncio
async def test_openai_default_max_tokens_used_when_not_declared(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """OpenAI SDK default_max_tokens used as projection when call omits max_tokens."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="default-proj-oai", per_session_tokens=1000))
    await sdk.cost.track("default-proj-oai", sid, tokens=800)

    class FakeConfig:
        default_max_tokens = 500
        warn_on_no_wrappers = False

    sdk.config = FakeConfig()  # type: ignore[attr-defined]

    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeAsyncClient(response)
    wrap_openai(client, sdk=sdk, agent_id="default-proj-oai", session_id=sid)  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded):
        await client.chat.completions.create(model="gpt-4o")  # no explicit max_tokens


# ---------------------------------------------------------------------------
# Item 4: OpenAI streaming tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_streaming_budget_gate_blocks_before_stream(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """OpenAI streaming: max_tokens would breach limit → BudgetExceeded, stream never opened."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="stream-gate-oai", per_session_tokens=1000))
    await sdk.cost.track("stream-gate-oai", sid, tokens=800)

    stream_opened = False

    class FakeStream:
        def __init__(self) -> None:
            nonlocal stream_opened
            stream_opened = True

    class StreamingCompletions:
        async def create(self, **kwargs: Any) -> FakeStream:
            return FakeStream()

    class StreamingChat:
        completions = StreamingCompletions()

    class StreamingClient:
        chat = StreamingChat()

    client = StreamingClient()
    wrap_openai(client, sdk=sdk, agent_id="stream-gate-oai", session_id=sid)  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded):
        await client.chat.completions.create(model="gpt-4o", stream=True, max_tokens=300)

    assert not stream_opened, "Stream must not be opened when budget gate fires"


@pytest.mark.asyncio
async def test_openai_streaming_call_tracks_cost(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """OpenAI streaming call → track() called with projected tokens."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="stream-track-oai", per_session_tokens=5000))

    class FakeStream:
        pass

    class StreamingCompletions:
        async def create(self, **kwargs: Any) -> FakeStream:
            return FakeStream()

    class StreamingChat:
        completions = StreamingCompletions()

    class StreamingClient:
        chat = StreamingChat()

    client = StreamingClient()
    wrap_openai(client, sdk=sdk, agent_id="stream-track-oai", session_id=sid)  # type: ignore[arg-type]

    await client.chat.completions.create(model="gpt-4o", stream=True, max_tokens=200)

    snap = await sdk.cost.snapshot("stream-track-oai", sid)
    assert snap.session_tokens_used == 200


@pytest.mark.asyncio
async def test_openai_streaming_audit_event_cost_tracked_true(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """OpenAI streaming with max_tokens → llm.result must not have cost_tracked=False."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="stream-audit-oai", per_session_tokens=5000))

    class FakeStream:
        pass

    class StreamingCompletions:
        async def create(self, **kwargs: Any) -> FakeStream:
            return FakeStream()

    class StreamingChat:
        completions = StreamingCompletions()

    class StreamingClient:
        chat = StreamingChat()

    client = StreamingClient()
    wrap_openai(client, sdk=sdk, agent_id="stream-audit-oai", session_id=sid)  # type: ignore[arg-type]

    await client.chat.completions.create(model="gpt-4o", stream=True, max_tokens=150)
    await sdk.audit._writer.flush()

    all_events = list(store._events.values())
    result_events = [e for e in all_events if e.kind == "llm.result"]
    assert len(result_events) >= 1

    for event in result_events:
        assert event.metadata.get("cost_tracked") is not False, (
            f"Streaming audit event must not have cost_tracked=False, got: {event.metadata}"
        )
