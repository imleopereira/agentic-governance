"""Behavior tests for v0.6.2 Track C Bug #8 (streaming token reconciliation).

The pre-fix wrapper trusted ``max_tokens`` as the tracked amount for the
lifetime of the stream. A provider that returned 10k tokens with
``max_tokens=10`` undercounted by 1000x. This is a silent USD budget bypass.

Fix contract:
  * On stream start, ``projected_tokens`` (max_tokens) is tracked.
  * On stream end, the provider's final usage is read and the DELTA is
    tracked via :meth:`CostModule.reconcile`. Counters are monotonic — a
    smaller-than-projected actual never refunds, but a warning is logged.

These tests exercise the Anthropic and OpenAI wrappers against fake
streams that expose a final-usage block. No source-grep asserts.
"""
from __future__ import annotations

import secrets
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.cost import BudgetPolicy, CostModule
from codeatelier_governance.integrations.anthropic_wrap import wrap_anthropic
from codeatelier_governance.integrations.openai_wrap import wrap_openai
from codeatelier_governance.scope.module import ScopeModule


class FakeSDK:
    def __init__(
        self,
        audit: AuditModule,
        scope: ScopeModule,
        cost: CostModule,
    ) -> None:
        self.audit = audit
        self.scope = scope
        self.cost = cost


@pytest_asyncio.fixture
async def store() -> InMemoryAuditStore:
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def audit(store: InMemoryAuditStore) -> AuditModule:
    writer = BatchingWriter(
        primary=store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    module = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    await module.start()
    try:
        yield module  # type: ignore[misc]
    finally:
        await module.close()


@pytest_asyncio.fixture
async def sdk(audit: AuditModule) -> FakeSDK:
    scope = ScopeModule(audit)
    # Lax cost to tolerate unknown model names in test doubles.
    cost = CostModule(
        audit,
        strict_unknown_models=False,
        unknown_model_fallback_usd_per_million=0.0,
    )
    return FakeSDK(audit=audit, scope=scope, cost=cost)


# ===========================================================================
# Anthropic streaming reconciliation
# ===========================================================================


class FakeAnthUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeAnthFinalMessage:
    def __init__(self, usage: FakeAnthUsage) -> None:
        self.usage = usage


class FakeAnthStream:
    """Fake Anthropic stream exposing get_final_message() and async iter."""

    def __init__(self, final_usage: FakeAnthUsage, chunks: int = 3) -> None:
        self._final = FakeAnthFinalMessage(final_usage)
        self._chunks = chunks

    def get_final_message(self) -> FakeAnthFinalMessage:
        return self._final

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for i in range(self._chunks):
                yield f"chunk-{i}"
        return _gen()


class FakeAnthAsyncMessages:
    def __init__(self, stream: FakeAnthStream) -> None:
        self._stream = stream

    async def create(self, **kwargs: Any) -> FakeAnthStream:
        return self._stream


class FakeAnthAsyncClient:
    def __init__(self, stream: FakeAnthStream) -> None:
        self.messages = FakeAnthAsyncMessages(stream)


@pytest.mark.asyncio
async def test_bug8_anthropic_stream_reconciles_to_actual_usage(
    sdk: FakeSDK,
) -> None:
    """The pre-fix bug: projected=10, actual=1000 → tracked 10. Fix: tracked 1000."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="recon", per_session_tokens=100_000))

    # Stream declares max_tokens=10 but returns 1000 actual tokens
    stream = FakeAnthStream(FakeAnthUsage(input_tokens=200, output_tokens=800))
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="recon", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.messages.create(
        model="claude-sonnet-4-6", stream=True, max_tokens=10,
    )

    # Consume the proxy stream — triggers reconciliation via the aiter finally.
    async for _ in proxy:
        pass

    snap = await sdk.cost.snapshot("recon", sid)
    # Before fix: 10. After fix: 10 (projected) + (1000 - 10) reconciled = 1000 total.
    assert snap.session_tokens_used == 1000, (
        f"Expected 1000 actual tokens, got {snap.session_tokens_used} — "
        f"reconciliation did not run."
    )


@pytest.mark.asyncio
async def test_bug8_anthropic_reconcile_audit_event_emitted(
    sdk: FakeSDK, store: InMemoryAuditStore,
) -> None:
    """Reconciliation must emit an llm.result.reconciled audit event."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="recon-audit", per_session_tokens=100_000))

    stream = FakeAnthStream(FakeAnthUsage(input_tokens=100, output_tokens=400))
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="recon-audit", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.messages.create(
        model="claude-sonnet-4-6", stream=True, max_tokens=50,
    )
    async for _ in proxy:
        pass

    await sdk.audit._writer.flush()

    reconciled_events = [
        e for e in store._events.values() if e.kind == "llm.result.reconciled"
    ]
    assert len(reconciled_events) == 1
    meta = reconciled_events[0].metadata
    assert meta["projected_tokens"] == 50
    assert meta["actual_tokens"] == 500
    assert meta["token_delta"] == 450


@pytest.mark.asyncio
async def test_bug8_anthropic_stream_reconcile_budget_trip_post_stream(
    sdk: FakeSDK,
) -> None:
    """Large actual usage must trip a budget cap on the NEXT check after reconcile."""
    sid = uuid4()
    # Cap set low enough that max_tokens=10 projection passes but
    # actual 5000 tokens + reconciliation pushes us over.
    sdk.cost.register(BudgetPolicy(agent_id="trip", per_session_tokens=1000))

    stream = FakeAnthStream(FakeAnthUsage(input_tokens=2000, output_tokens=3000))
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="trip", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.messages.create(
        model="claude-sonnet-4-6", stream=True, max_tokens=10,
    )
    async for _ in proxy:
        pass

    snap = await sdk.cost.snapshot("trip", sid)
    assert snap.session_tokens_used == 5000

    from codeatelier_governance.cost import BudgetExceeded

    with pytest.raises(BudgetExceeded, match="per_session_tokens"):
        await sdk.cost.check_or_raise("trip", sid)


@pytest.mark.asyncio
async def test_bug8_anthropic_negative_delta_does_not_refund(sdk: FakeSDK) -> None:
    """Actual < projected: no refund (counters monotonic), warning logged."""
    import structlog.testing

    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="mono", per_session_tokens=100_000))

    # Projected 1000, actual 100 — the wrapper must not refund.
    stream = FakeAnthStream(FakeAnthUsage(input_tokens=50, output_tokens=50))
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="mono", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap:
        proxy = await client.messages.create(
            model="claude-sonnet-4-6", stream=True, max_tokens=1000,
        )
        async for _ in proxy:
            pass

    snap = await sdk.cost.snapshot("mono", sid)
    # Must still be 1000 (the projected amount); no refund even though
    # actual was 100.
    assert snap.session_tokens_used == 1000
    # Warning must be emitted explaining no refund.
    negative_warnings = [
        e for e in cap
        if e.get("event") == "cost.reconcile_negative_delta_ignored"
    ]
    assert len(negative_warnings) >= 1


@pytest.mark.asyncio
async def test_bug8_anthropic_stream_without_final_usage_falls_back(
    sdk: FakeSDK,
) -> None:
    """If the stream can't expose final usage, tracked remains projected + warning."""
    import structlog.testing

    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="nousage", per_session_tokens=100_000))

    class UsagelessStream:
        def __aiter__(self) -> Any:
            async def _gen() -> Any:
                yield "chunk-0"
            return _gen()

    stream = UsagelessStream()
    client = FakeAnthAsyncClient(stream)  # type: ignore[arg-type]
    wrap_anthropic(client, sdk=sdk, agent_id="nousage", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap:
        proxy = await client.messages.create(
            model="claude-sonnet-4-6", stream=True, max_tokens=25,
        )
        async for _ in proxy:
            pass

    snap = await sdk.cost.snapshot("nousage", sid)
    assert snap.session_tokens_used == 25  # still projected
    assert any(
        e.get("event") == "governance.anthropic_wrap.stream_usage_unavailable"
        for e in cap
    )


# ===========================================================================
# OpenAI streaming reconciliation
# ===========================================================================


class FakeOpenAIUsage:
    def __init__(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int | None = None,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = (
            total_tokens if total_tokens is not None else prompt_tokens + completion_tokens
        )


class FakeOpenAIChunk:
    """A streaming chunk. Only the final chunk carries `usage`."""

    def __init__(self, usage: FakeOpenAIUsage | None = None) -> None:
        self.usage = usage


class FakeOpenAIStream:
    """Fake OpenAI stream: async iter yielding chunks; last has usage."""

    def __init__(self, final_usage: FakeOpenAIUsage, n_chunks: int = 3) -> None:
        self._final_usage = final_usage
        self._n_chunks = n_chunks

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for _ in range(self._n_chunks - 1):
                yield FakeOpenAIChunk()
            yield FakeOpenAIChunk(usage=self._final_usage)
        return _gen()


class FakeOpenAICompletions:
    def __init__(self, stream: FakeOpenAIStream) -> None:
        self._stream = stream

    async def create(self, **kwargs: Any) -> FakeOpenAIStream:
        return self._stream


class FakeOpenAIChat:
    def __init__(self, stream: FakeOpenAIStream) -> None:
        self.completions = FakeOpenAICompletions(stream)


class FakeAsyncOpenAIClient:
    """Naming contains 'Async' so wrap_openai picks the async path."""

    def __init__(self, stream: FakeOpenAIStream) -> None:
        self.chat = FakeOpenAIChat(stream)


@pytest.mark.asyncio
async def test_bug8_openai_stream_reconciles_to_actual_usage(sdk: FakeSDK) -> None:
    """OpenAI variant: projected=10, actual=1000 → tracked 1000."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="oai-recon", per_session_tokens=100_000))

    stream = FakeOpenAIStream(
        FakeOpenAIUsage(prompt_tokens=100, completion_tokens=900, total_tokens=1000),
    )
    client = FakeAsyncOpenAIClient(stream)
    wrap_openai(client, sdk=sdk, agent_id="oai-recon", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.chat.completions.create(
        model="gpt-4o", stream=True, max_tokens=10,
        stream_options={"include_usage": True},
    )
    async for _ in proxy:
        pass

    snap = await sdk.cost.snapshot("oai-recon", sid)
    assert snap.session_tokens_used == 1000, (
        f"Expected 1000 reconciled tokens, got {snap.session_tokens_used}"
    )


@pytest.mark.asyncio
async def test_bug8_openai_stream_without_include_usage_warns(sdk: FakeSDK) -> None:
    """If caller didn't pass include_usage, reconcile logs a warning and leaves projected."""
    import structlog.testing

    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="oai-nouse", per_session_tokens=100_000))

    class UsagelessOpenAIStream:
        def __aiter__(self) -> Any:
            async def _gen() -> Any:
                yield FakeOpenAIChunk()  # no usage on any chunk
                yield FakeOpenAIChunk()
            return _gen()

    stream = UsagelessOpenAIStream()
    client = FakeAsyncOpenAIClient(stream)  # type: ignore[arg-type]
    wrap_openai(client, sdk=sdk, agent_id="oai-nouse", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap:
        proxy = await client.chat.completions.create(
            model="gpt-4o", stream=True, max_tokens=40,
        )
        async for _ in proxy:
            pass

    snap = await sdk.cost.snapshot("oai-nouse", sid)
    assert snap.session_tokens_used == 40  # projected only
    assert any(
        e.get("event") == "governance.openai_wrap.stream_usage_unavailable"
        for e in cap
    )


@pytest.mark.asyncio
async def test_bug8_openai_reconcile_trips_budget_post_stream(sdk: FakeSDK) -> None:
    """Actual usage pushes OpenAI caller over the token cap on the next check."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="oai-trip", per_session_tokens=1000))

    stream = FakeOpenAIStream(
        FakeOpenAIUsage(prompt_tokens=1000, completion_tokens=4000, total_tokens=5000),
    )
    client = FakeAsyncOpenAIClient(stream)
    wrap_openai(client, sdk=sdk, agent_id="oai-trip", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.chat.completions.create(
        model="gpt-4o", stream=True, max_tokens=10,
        stream_options={"include_usage": True},
    )
    async for _ in proxy:
        pass

    snap = await sdk.cost.snapshot("oai-trip", sid)
    assert snap.session_tokens_used == 5000

    from codeatelier_governance.cost import BudgetExceeded

    with pytest.raises(BudgetExceeded, match="per_session_tokens"):
        await sdk.cost.check_or_raise("oai-trip", sid)
