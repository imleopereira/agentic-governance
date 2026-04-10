"""Tests for the OpenAI wrap_openai adapter.

All OpenAI types are mocked; the openai package is NOT required for these tests.
"""
from __future__ import annotations

import secrets
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio

from codeatelier_governance.audit import AuditModule, BatchingWriter, InMemoryAuditStore
from codeatelier_governance.cost.errors import BudgetExceeded
from codeatelier_governance.cost.models import BudgetPolicy
from codeatelier_governance.cost.module import CostModule
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
async def test_wrap_sync_client_audit_events(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """wrap_openai on a sync client should log llm.call and llm.result.

    When called from within a running event loop (like pytest-asyncio), the
    sync wrapper returns a coroutine that must be awaited.
    """
    response = FakeResponse(usage=FakeUsage(10, 20, 30))
    client = FakeSyncClient(response)

    wrapped = wrap_openai(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]
    assert wrapped is client  # Same object returned

    # Inside an event loop the sync wrapper yields a coroutine.
    result = await wrapped.chat.completions.create(model="gpt-4o")
    assert result is response

    count = await store.count()
    # At least llm.call + llm.result = 2 events
    assert count >= 2


@pytest.mark.asyncio
async def test_wrap_sync_client_error_audit(
    sdk: FakeSDK, store: InMemoryAuditStore
) -> None:
    """On LLM error, wrap_openai should log llm.error and re-raise."""
    response = FakeResponse()
    client = FakeSyncClient(response)
    # Make the original create raise
    client.chat.completions = FakeCompletions(response)
    client.chat.completions.create = MagicMock(side_effect=RuntimeError("API error"))  # type: ignore[method-assign]

    wrapped = wrap_openai(client, sdk=sdk, agent_id="test-agent")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="API error"):
        await wrapped.chat.completions.create(model="gpt-4o")

    # Should have logged llm.call + llm.error
    count = await store.count()
    assert count >= 2


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
