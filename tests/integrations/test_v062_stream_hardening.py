"""v0.6.2-followup — hardening tests for streaming reconciliation bypasses.

The v0.6.2 Bug #8 fix added a reconciliation proxy, but a DA review found
four bypass vectors:

1. **Abandonment** — proxy dropped without iteration or context entry.
2. **Cancellation / mid-stream exception** — provider tears down before
   exposing final usage; partial-usage fallback needed.
3. **Double-reconcile** — ``async with s as t: async for chunk in t:`` fires
   reconcile twice (inner aiter finally + outer aexit).
4. **OpenAI silent under-count** — wrapper did not auto-inject
   ``stream_options={include_usage: True}``.

Plus two ancillary tripwires: ``isinstance(proxy, OrigStreamType)`` must hold
(customers check this), and sync ``__exit__`` must not lose the reconcile task
to GC.

All tests are BEHAVIOUR tests — they exercise the public API end to end and
would have PASSED against the pre-fix wrapper only by accident, and in most
cases FAIL cleanly without the fix (see comments marked WOULD_FAIL).
"""
from __future__ import annotations

import asyncio
import gc
import secrets
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import structlog.testing

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
        primary=store, batch_size=5, flush_interval_s=0.02, buffer_max=1000,
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
    cost = CostModule(
        audit,
        strict_unknown_models=False,
        unknown_model_fallback_usd_per_million=0.0,
    )
    return FakeSDK(audit=audit, scope=scope, cost=cost)


# ===========================================================================
# Shared fakes
# ===========================================================================


class FakeAnthUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeAnthFinalMessage:
    def __init__(self, usage: FakeAnthUsage) -> None:
        self.usage = usage


class FakeAnthStream:
    """Fake Anthropic stream with get_final_message() + async iter."""

    def __init__(
        self,
        final_usage: FakeAnthUsage | None,
        chunks: int = 3,
        raise_at: int | None = None,
        raise_exc: type[BaseException] = RuntimeError,
    ) -> None:
        self._final = (
            FakeAnthFinalMessage(final_usage) if final_usage is not None else None
        )
        self._chunks = chunks
        self._raise_at = raise_at
        self._raise_exc = raise_exc

    def get_final_message(self) -> FakeAnthFinalMessage | None:
        return self._final

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for i in range(self._chunks):
                if self._raise_at is not None and i == self._raise_at:
                    raise self._raise_exc("mid-stream failure")
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


class FakeOpenAIUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class FakeOpenAIChunk:
    def __init__(self, usage: FakeOpenAIUsage | None = None) -> None:
        self.usage = usage


class FakeOpenAIStream:
    def __init__(
        self,
        final_usage: FakeOpenAIUsage | None,
        n_chunks: int = 3,
        raise_at: int | None = None,
        raise_exc: type[BaseException] = RuntimeError,
    ) -> None:
        self._final_usage = final_usage
        self._n_chunks = n_chunks
        self._raise_at = raise_at
        self._raise_exc = raise_exc

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for i in range(self._n_chunks - 1):
                if self._raise_at is not None and i == self._raise_at:
                    raise self._raise_exc("mid-stream failure")
                yield FakeOpenAIChunk()
            if self._final_usage is not None:
                yield FakeOpenAIChunk(usage=self._final_usage)
        return _gen()


class FakeOpenAICompletions:
    def __init__(self, stream: FakeOpenAIStream) -> None:
        self._stream = stream
        self.last_kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> FakeOpenAIStream:
        self.last_kwargs = dict(kwargs)
        return self._stream


class FakeOpenAIChat:
    def __init__(self, stream: FakeOpenAIStream) -> None:
        self.completions = FakeOpenAICompletions(stream)


class FakeAsyncOpenAIClient:
    def __init__(self, stream: FakeOpenAIStream) -> None:
        self.chat = FakeOpenAIChat(stream)


# ===========================================================================
# Bypass #1 — Abandonment
# ===========================================================================


@pytest.mark.asyncio
async def test_abandoned_anthropic_stream_emits_warn(sdk: FakeSDK) -> None:
    """Proxy dropped without iteration or ctx entry must emit WARN on GC.

    Proves: weakref.finalize detects abandonment. WOULD_FAIL without the fix —
    the pre-followup wrapper had no __del__ / finalize and silently left the
    projected amount as the tracked total.
    """
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="aband", per_session_tokens=100_000))

    stream = FakeAnthStream(FakeAnthUsage(input_tokens=50, output_tokens=50))
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="aband", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap:
        proxy = await client.messages.create(
            model="claude-sonnet-4-6", stream=True, max_tokens=100,
        )
        # Drop the reference without iterating
        del proxy
        # Force GC deterministically
        gc.collect()
        # Finalizer may enqueue log synchronously; a small sleep lets any
        # loop-scheduled work finish (there shouldn't be any, but be safe).
        await asyncio.sleep(0)

    events = [
        e for e in cap
        if e.get("event") == "governance.stream.abandoned_without_reconcile"
    ]
    assert len(events) == 1, f"expected 1 abandonment WARN, got {events!r}"
    assert events[0]["agent_id"] == "aband"
    assert events[0]["projected_tokens"] == 100


@pytest.mark.asyncio
async def test_abandoned_openai_stream_emits_warn(sdk: FakeSDK) -> None:
    """OpenAI variant of the abandonment WARN. Proves the finalizer is
    installed on both wrappers."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="oai-aband", per_session_tokens=100_000))

    stream = FakeOpenAIStream(
        FakeOpenAIUsage(prompt_tokens=10, completion_tokens=10),
    )
    client = FakeAsyncOpenAIClient(stream)
    wrap_openai(client, sdk=sdk, agent_id="oai-aband", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap:
        proxy = await client.chat.completions.create(
            model="gpt-4o", stream=True, max_tokens=50,
        )
        del proxy
        gc.collect()
        await asyncio.sleep(0)

    events = [
        e for e in cap
        if e.get("event") == "governance.stream.abandoned_without_reconcile"
    ]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_iterated_stream_does_not_emit_abandonment_warn(sdk: FakeSDK) -> None:
    """After normal iteration, finalizer must be a no-op.

    Proves the finalizer is gated on state['reconciled'] — the normal path
    does not double-fire the WARN.
    """
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="iter-ok", per_session_tokens=100_000))

    stream = FakeAnthStream(FakeAnthUsage(input_tokens=20, output_tokens=30))
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="iter-ok", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap:
        proxy = await client.messages.create(
            model="claude-sonnet-4-6", stream=True, max_tokens=25,
        )
        async for _ in proxy:
            pass
        del proxy
        gc.collect()
        await asyncio.sleep(0)

    abandonment_events = [
        e for e in cap
        if e.get("event") == "governance.stream.abandoned_without_reconcile"
    ]
    assert len(abandonment_events) == 0


# ===========================================================================
# Bypass #2 — Cancellation & mid-stream exception
# ===========================================================================


@pytest.mark.asyncio
async def test_mid_stream_exception_triggers_estimate_reconcile(
    sdk: FakeSDK,
) -> None:
    """Provider raises mid-stream. Final usage unavailable (None). Chunks already
    yielded drive the estimate. Tracked ≥ projected (never under).

    Proves the chunk_estimate fallback path. WOULD_FAIL without the fix —
    the old wrapper returned early on usage=None leaving tracked at projected.
    """
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="mid-exc", per_session_tokens=100_000))

    # final_usage=None -> get_final_message returns None -> extract returns None.
    # 2 chunks delivered before raise at index 2.
    stream = FakeAnthStream(final_usage=None, chunks=5, raise_at=2)
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="mid-exc", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap:
        proxy = await client.messages.create(
            model="claude-sonnet-4-6", stream=True, max_tokens=10,
        )
        with pytest.raises(RuntimeError, match="mid-stream failure"):
            async for _ in proxy:
                pass

    snap = await sdk.cost.snapshot("mid-exc", sid)
    # 2 chunks * 8 (STREAM_CHUNK_TOKEN_ESTIMATE) = 16; floor = projected (10).
    # max(10, 16) = 16. Tracked = 10 (projected) + delta-to-16 = 16.
    assert snap.session_tokens_used >= 10, (
        "tracked under projected — refund bug"
    )
    assert snap.session_tokens_used == 16, (
        f"expected estimate 16 tokens, got {snap.session_tokens_used}"
    )
    assert any(
        e.get("event") == "governance.anthropic_wrap.stream_usage_estimated"
        for e in cap
    )


@pytest.mark.asyncio
async def test_cancellation_reconciles_and_reraises(sdk: FakeSDK) -> None:
    """Cancellation mid-iter must both reconcile AND re-raise CancelledError.

    Proves: (a) CancelledError propagates (so asyncio cancellation works),
    (b) reconcile still fires with torn_down=True. WOULD_FAIL without the
    fix — `except Exception` never caught CancelledError, so the finally
    called reconcile but the reconcile silently returned on None usage.
    """
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="cancel", per_session_tokens=100_000))

    stream = FakeAnthStream(
        final_usage=None, chunks=10, raise_at=3,
        raise_exc=asyncio.CancelledError,
    )
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="cancel", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.messages.create(
        model="claude-sonnet-4-6", stream=True, max_tokens=5,
    )
    with pytest.raises(asyncio.CancelledError):
        async for _ in proxy:
            pass

    snap = await sdk.cost.snapshot("cancel", sid)
    # 3 chunks yielded * 8 = 24; floor at projected 5; max(5, 24) = 24.
    assert snap.session_tokens_used == 24


# ===========================================================================
# Bypass #3 — Double-reconcile
# ===========================================================================


class FakeAnthCtxStream:
    """Anthropic ctx-manager stream: `async with client.messages.stream(...)`."""

    def __init__(self, final_usage: FakeAnthUsage, chunks: int = 2) -> None:
        self._final = FakeAnthFinalMessage(final_usage)
        self._chunks = chunks

    def get_final_message(self) -> FakeAnthFinalMessage:
        return self._final

    async def __aenter__(self) -> "FakeAnthCtxStream":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for i in range(self._chunks):
                yield f"chunk-{i}"
        return _gen()


class FakeAnthAsyncMessagesCtx:
    def __init__(self, stream: FakeAnthCtxStream) -> None:
        self._stream = stream

    async def create(self, **kwargs: Any) -> FakeAnthCtxStream:
        return self._stream


class FakeAnthAsyncClientCtx:
    def __init__(self, stream: FakeAnthCtxStream) -> None:
        self.messages = FakeAnthAsyncMessagesCtx(stream)


@pytest.mark.asyncio
async def test_async_with_plus_aiter_reconciles_exactly_once(
    sdk: FakeSDK, store: InMemoryAuditStore,
) -> None:
    """`async with s as t: async for c in t:` must reconcile exactly once.

    Proves state sharing across inner/outer proxies. WOULD_FAIL without the fix —
    each proxy had its own state dict so the delta was double-added.
    """
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="once", per_session_tokens=100_000))

    stream = FakeAnthCtxStream(FakeAnthUsage(input_tokens=100, output_tokens=400))
    client = FakeAnthAsyncClientCtx(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="once", session_id=sid)  # type: ignore[arg-type]

    outer_proxy = await client.messages.create(
        model="claude-sonnet-4-6", stream=True, max_tokens=50,
    )

    async with outer_proxy as inner_proxy:
        async for _ in inner_proxy:
            pass

    await sdk.audit._writer.flush()

    # EXACTLY ONE reconciled event — double-reconcile bug would produce 2.
    reconciled_events = [
        e for e in store._events.values() if e.kind == "llm.result.reconciled"
    ]
    assert len(reconciled_events) == 1, (
        f"expected 1 reconciled event, got {len(reconciled_events)} — "
        f"double-reconcile bug"
    )

    snap = await sdk.cost.snapshot("once", sid)
    # Projected 50, actual 500. Delta 450 added ONCE. Total = 500.
    assert snap.session_tokens_used == 500, (
        f"expected 500 (reconciled once), got {snap.session_tokens_used}"
    )


# ===========================================================================
# Bypass #4 — OpenAI auto-inject include_usage
# ===========================================================================


@pytest.mark.asyncio
async def test_openai_auto_injects_include_usage(sdk: FakeSDK) -> None:
    """Wrapper adds ``stream_options={'include_usage': True}`` when absent.

    Proves: caller omits stream_options, wrapper injects it, OpenAI emits
    final usage chunk, reconcile reads it. WOULD_FAIL without the fix —
    no auto-inject meant reconcile never got final usage and tracked stayed
    at projected.
    """
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="auto", per_session_tokens=100_000))

    stream = FakeOpenAIStream(
        FakeOpenAIUsage(prompt_tokens=200, completion_tokens=800),
    )
    client = FakeAsyncOpenAIClient(stream)
    wrap_openai(client, sdk=sdk, agent_id="auto", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.chat.completions.create(
        model="gpt-4o", stream=True, max_tokens=10,
        # NOTE: no stream_options set
    )
    async for _ in proxy:
        pass

    # Wrapper must have injected stream_options
    kwargs = client.chat.completions.last_kwargs
    assert kwargs is not None
    assert kwargs.get("stream_options") == {"include_usage": True}

    # And reconcile actually used the final usage (1000 not 10).
    snap = await sdk.cost.snapshot("auto", sid)
    assert snap.session_tokens_used == 1000


@pytest.mark.asyncio
async def test_openai_respects_explicit_include_usage_true(sdk: FakeSDK) -> None:
    """Caller sets include_usage=True explicitly: wrapper preserves it,
    no warning."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="explicit", per_session_tokens=100_000))

    stream = FakeOpenAIStream(
        FakeOpenAIUsage(prompt_tokens=10, completion_tokens=90),
    )
    client = FakeAsyncOpenAIClient(stream)
    wrap_openai(client, sdk=sdk, agent_id="explicit", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap:
        proxy = await client.chat.completions.create(
            model="gpt-4o", stream=True, max_tokens=50,
            stream_options={"include_usage": True, "extra_flag": "keep"},
        )
        async for _ in proxy:
            pass

    kwargs = client.chat.completions.last_kwargs
    assert kwargs is not None
    # Other fields preserved
    assert kwargs["stream_options"]["include_usage"] is True
    assert kwargs["stream_options"]["extra_flag"] == "keep"
    # No warnings about override
    assert not any(
        e.get("event") == "governance.openai_wrap.include_usage_explicitly_disabled"
        for e in cap
    )


@pytest.mark.asyncio
async def test_openai_respects_explicit_include_usage_false_with_warn(
    sdk: FakeSDK,
) -> None:
    """Caller sets include_usage=False: wrapper honors it but emits WARN.

    Proves we never override explicit user intent but make the cost visible.
    """
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="no-usage", per_session_tokens=100_000))

    # Stream with NO final usage (because caller disabled include_usage).
    stream = FakeOpenAIStream(final_usage=None, n_chunks=2)
    client = FakeAsyncOpenAIClient(stream)
    wrap_openai(client, sdk=sdk, agent_id="no-usage", session_id=sid)  # type: ignore[arg-type]

    with structlog.testing.capture_logs() as cap:
        proxy = await client.chat.completions.create(
            model="gpt-4o", stream=True, max_tokens=40,
            stream_options={"include_usage": False},
        )
        async for _ in proxy:
            pass

    kwargs = client.chat.completions.last_kwargs
    assert kwargs is not None
    # User intent preserved
    assert kwargs["stream_options"]["include_usage"] is False
    # WARN emitted
    assert any(
        e.get("event") == "governance.openai_wrap.include_usage_explicitly_disabled"
        for e in cap
    )


# ===========================================================================
# Ancillary: isinstance transparency
# ===========================================================================


class NamedAnthStream(FakeAnthStream):
    """Distinct subclass so isinstance asserts are meaningful."""


@pytest.mark.asyncio
async def test_proxy_preserves_isinstance_of_wrapped_type(sdk: FakeSDK) -> None:
    """Customers do ``isinstance(stream, anthropic.MessageStream)`` —
    after wrapping, that check MUST still pass or they'll strip the wrapper.

    Proves __class__ property makes the proxy polymorphic with the wrapped type.
    """
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="iso", per_session_tokens=100_000))

    stream = NamedAnthStream(FakeAnthUsage(input_tokens=5, output_tokens=5))
    client = FakeAnthAsyncClient(stream)  # type: ignore[arg-type]
    wrap_anthropic(client, sdk=sdk, agent_id="iso", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.messages.create(
        model="claude-sonnet-4-6", stream=True, max_tokens=5,
    )

    # The load-bearing assertion: isinstance transparency.
    assert isinstance(proxy, NamedAnthStream), (
        "proxy must pass isinstance(OriginalStreamType) — customers rely on it"
    )

    # Consume so finalizer doesn't WARN.
    async for _ in proxy:
        pass


# ===========================================================================
# Reconciliation source field in audit
# ===========================================================================


@pytest.mark.asyncio
async def test_reconciled_audit_carries_provider_final_source(
    sdk: FakeSDK, store: InMemoryAuditStore,
) -> None:
    """Normal path: audit event's ``reconciliation_source`` = 'provider_final'."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="src-ok", per_session_tokens=100_000))

    stream = FakeAnthStream(FakeAnthUsage(input_tokens=10, output_tokens=90))
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="src-ok", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.messages.create(
        model="claude-sonnet-4-6", stream=True, max_tokens=5,
    )
    async for _ in proxy:
        pass

    await sdk.audit._writer.flush()
    events = [
        e for e in store._events.values() if e.kind == "llm.result.reconciled"
    ]
    assert len(events) == 1
    assert events[0].metadata.get("reconciliation_source") == "provider_final"


@pytest.mark.asyncio
async def test_reconciled_audit_carries_chunk_estimate_source(
    sdk: FakeSDK, store: InMemoryAuditStore,
) -> None:
    """Torn-down path: audit event's ``reconciliation_source`` = 'chunk_estimate'."""
    sid = uuid4()
    sdk.cost.register(BudgetPolicy(agent_id="src-est", per_session_tokens=100_000))

    stream = FakeAnthStream(final_usage=None, chunks=5, raise_at=2)
    client = FakeAnthAsyncClient(stream)
    wrap_anthropic(client, sdk=sdk, agent_id="src-est", session_id=sid)  # type: ignore[arg-type]

    proxy = await client.messages.create(
        model="claude-sonnet-4-6", stream=True, max_tokens=5,
    )
    with pytest.raises(RuntimeError):
        async for _ in proxy:
            pass

    await sdk.audit._writer.flush()
    events = [
        e for e in store._events.values() if e.kind == "llm.result.reconciled"
    ]
    assert len(events) == 1
    assert events[0].metadata.get("reconciliation_source") == "chunk_estimate"
