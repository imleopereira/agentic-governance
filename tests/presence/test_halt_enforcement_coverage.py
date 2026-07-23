"""v0.6.2 P0 — halt enforcement coverage across every SDK gate.

v0.5.4 shipped the halt switch but only wired it into ``scope.check``.
``cost.check_or_raise``, ``gates.request``, and the ``wrap_openai`` /
``wrap_anthropic`` wrappers retained a bypass: a halted agent could keep
burning budget, minting HITL tokens, and making LLM calls.

This suite is the regression-proof for the fix. Each enforcement path is
tested twice:

  * halted agent   -> must raise ``AgentHaltedError``
  * unhalted agent -> must pass the halt check (may still fail downstream
                      for unrelated reasons, but never ``AgentHaltedError``)

Uses the in-memory ``PresenceModule`` — no Docker, no Postgres.
"""
from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.cost.module import CostModule
from codeatelier_governance.cost.models import BudgetPolicy
from codeatelier_governance.gates.module import GatesModule
from codeatelier_governance.integrations.anthropic_wrap import wrap_anthropic
from codeatelier_governance.integrations.openai_wrap import wrap_openai
from codeatelier_governance.presence import AgentHaltedError, PresenceModule
from codeatelier_governance.presence.models import AgentStatus
from codeatelier_governance.scope.module import ScopeModule


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _halt_in_memory(presence: PresenceModule, agent_id: str) -> None:
    """Halt an agent in the in-memory store via the dedicated marker.

    Mirrors the helper in ``test_halt_switch.py``: sets the top-level
    ``halted_by`` / ``halted_at`` / ``halt_reason`` keys (the in-memory
    analogue of the revoke-protected DB columns), behaviour-locked to the
    v0.7 columns-only marker schema.
    """
    async with presence._lock:
        if agent_id not in presence._agents:
            raise RuntimeError(
                f"agent {agent_id!r} not in presence; call heartbeat() first"
            )
        presence._agents[agent_id]["halted_by"] = "test-operator"
        presence._agents[agent_id]["halted_at"] = datetime.now(timezone.utc).isoformat()
        presence._agents[agent_id]["halt_reason"] = "v0.6.2 halt enforcement coverage"
        presence._agents[agent_id]["status"] = AgentStatus.UNRESPONSIVE.value
    await presence.force_refresh_halted_cache()


# ---------------------------------------------------------------------------
# Fake LLM clients — reused from the wrap_* test suites
# ---------------------------------------------------------------------------


class _FakeOpenAIUsage:
    def __init__(self) -> None:
        self.prompt_tokens = 10
        self.completion_tokens = 20
        self.total_tokens = 30


class _FakeOpenAIResponse:
    def __init__(self) -> None:
        self.usage = _FakeOpenAIUsage()


class _FakeOpenAIAsyncCompletions:
    async def create(self, **kwargs: Any) -> _FakeOpenAIResponse:
        return _FakeOpenAIResponse()


class _FakeOpenAIChat:
    def __init__(self) -> None:
        self.completions = _FakeOpenAIAsyncCompletions()


class _FakeAsyncOpenAIClient:
    def __init__(self) -> None:
        self.chat = _FakeOpenAIChat()


class _FakeAnthropicUsage:
    def __init__(self) -> None:
        self.input_tokens = 10
        self.output_tokens = 20


class _FakeAnthropicResponse:
    def __init__(self) -> None:
        self.model = "claude-sonnet-4-6"
        self.usage = _FakeAnthropicUsage()


class _FakeAnthropicAsyncMessages:
    async def create(self, **kwargs: Any) -> _FakeAnthropicResponse:
        return _FakeAnthropicResponse()


class _FakeAsyncAnthropicClient:
    def __init__(self) -> None:
        self.messages = _FakeAnthropicAsyncMessages()


# AsyncAnthropic detection in wrap_anthropic uses iscoroutinefunction on
# messages.create; AsyncAnthropicClient above satisfies it. For OpenAI we
# match either iscoroutinefunction OR "Async" in the class name.


class AsyncFakeOpenAIClient(_FakeAsyncOpenAIClient):
    """Class name carries 'Async' so wrap_openai routes to the async path."""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeSDK:
    """Minimal SDK stand-in; exposes only what the wrappers / modules read."""

    def __init__(
        self,
        *,
        audit: AuditModule,
        scope: ScopeModule,
        cost: CostModule,
        gates: GatesModule,
        presence: PresenceModule,
    ) -> None:
        self.audit = audit
        self.scope = scope
        self.cost = cost
        self.gates = gates
        self.presence = presence


@pytest_asyncio.fixture
async def sdk_with_presence() -> AsyncIterator[FakeSDK]:
    store = InMemoryAuditStore(max_events=10_000)
    secret = secrets.token_bytes(32)
    writer = BatchingWriter(
        primary=store, batch_size=5, flush_interval_s=0.02, buffer_max=1000,
    )
    audit = AuditModule(store, secret=secret, writer=writer)
    await audit.start()

    presence = PresenceModule()
    scope = ScopeModule(audit)
    scope.set_presence_module(presence)
    cost = CostModule(audit)
    cost.set_presence_module(presence)
    gates = GatesModule(audit, secret=secret)
    gates.set_presence_module(presence)

    fake = FakeSDK(
        audit=audit, scope=scope, cost=cost, gates=gates, presence=presence,
    )
    try:
        yield fake
    finally:
        await audit.close()


# ---------------------------------------------------------------------------
# cost.check_or_raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_check_or_raise_fails_closed_when_halted(
    sdk_with_presence: FakeSDK,
) -> None:
    agent_id = "halted-cost-agent"
    sdk_with_presence.cost.register(
        BudgetPolicy(agent_id=agent_id, per_session_usd=1.0)
    )
    await sdk_with_presence.presence.heartbeat(agent_id)
    await _halt_in_memory(sdk_with_presence.presence, agent_id)

    with pytest.raises(AgentHaltedError):
        await sdk_with_presence.cost.check_or_raise(agent_id, uuid4())


@pytest.mark.asyncio
async def test_cost_check_or_raise_passes_when_not_halted(
    sdk_with_presence: FakeSDK,
) -> None:
    agent_id = "live-cost-agent"
    sdk_with_presence.cost.register(
        BudgetPolicy(agent_id=agent_id, per_session_usd=1.0)
    )
    await sdk_with_presence.presence.heartbeat(agent_id)
    await sdk_with_presence.presence.force_refresh_halted_cache()

    # Must not raise AgentHaltedError (budget is fresh, so no BudgetExceeded either).
    await sdk_with_presence.cost.check_or_raise(agent_id, uuid4())


# ---------------------------------------------------------------------------
# gates.request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gates_request_fails_closed_when_halted(
    sdk_with_presence: FakeSDK,
) -> None:
    agent_id = "halted-gates-agent"
    await sdk_with_presence.presence.heartbeat(agent_id)
    await _halt_in_memory(sdk_with_presence.presence, agent_id)

    with pytest.raises(AgentHaltedError):
        await sdk_with_presence.gates.request(
            kind="patient.delete", agent_id=agent_id, payload={"id": 1},
        )


@pytest.mark.asyncio
async def test_gates_request_passes_when_not_halted(
    sdk_with_presence: FakeSDK,
) -> None:
    agent_id = "live-gates-agent"
    await sdk_with_presence.presence.heartbeat(agent_id)
    await sdk_with_presence.presence.force_refresh_halted_cache()

    req = await sdk_with_presence.gates.request(
        kind="patient.delete", agent_id=agent_id, payload={"id": 1},
    )
    assert req.agent_id == agent_id
    assert req.token  # token was minted


@pytest.mark.asyncio
async def test_gates_grant_on_pre_halt_token_still_works(
    sdk_with_presence: FakeSDK,
) -> None:
    """Edge case called out in v0.6.2 P0: operator can still resolve a gate
    issued BEFORE the halt. grant/deny are operator-facing and must not
    fail-close on a halted agent — the reviewer, not the agent, is the
    principal. This guards against regression where somebody wires the
    halt check into the wrong method.
    """
    agent_id = "to-be-halted-gates-agent"
    await sdk_with_presence.presence.heartbeat(agent_id)
    await sdk_with_presence.presence.force_refresh_halted_cache()

    # 1. Agent issues gate while live.
    req = await sdk_with_presence.gates.request(
        kind="refund.issue", agent_id=agent_id, payload={"amount": 100},
    )

    # 2. Operator halts the agent AFTER the gate was issued.
    await _halt_in_memory(sdk_with_presence.presence, agent_id)

    # 3. Reviewer resolves the pre-existing gate — MUST still succeed.
    await sdk_with_presence.gates.grant(req.token)

    # And a second agent request must now fail closed.
    with pytest.raises(AgentHaltedError):
        await sdk_with_presence.gates.request(
            kind="refund.issue", agent_id=agent_id, payload={"amount": 200},
        )


# ---------------------------------------------------------------------------
# wrap_openai
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_openai_fails_closed_when_halted(
    sdk_with_presence: FakeSDK,
) -> None:
    agent_id = "halted-openai-agent"
    await sdk_with_presence.presence.heartbeat(agent_id)
    await _halt_in_memory(sdk_with_presence.presence, agent_id)

    client = AsyncFakeOpenAIClient()
    wrapped = wrap_openai(client, sdk=sdk_with_presence, agent_id=agent_id)  # type: ignore[arg-type]

    with pytest.raises(AgentHaltedError):
        await wrapped.chat.completions.create(model="gpt-4o", max_tokens=32)


@pytest.mark.asyncio
async def test_wrap_openai_passes_when_not_halted(
    sdk_with_presence: FakeSDK,
) -> None:
    agent_id = "live-openai-agent"
    await sdk_with_presence.presence.heartbeat(agent_id)
    await sdk_with_presence.presence.force_refresh_halted_cache()

    client = AsyncFakeOpenAIClient()
    wrapped = wrap_openai(client, sdk=sdk_with_presence, agent_id=agent_id)  # type: ignore[arg-type]

    # No scope policy is registered — this exercises the exact bypass that
    # existed in v0.5.4 / v0.6.0 where scope.check (and therefore the halt
    # check) was skipped for unscoped agents.
    result = await wrapped.chat.completions.create(model="gpt-4o", max_tokens=32)
    assert result is not None


# ---------------------------------------------------------------------------
# wrap_anthropic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_anthropic_fails_closed_when_halted(
    sdk_with_presence: FakeSDK,
) -> None:
    agent_id = "halted-anthropic-agent"
    await sdk_with_presence.presence.heartbeat(agent_id)
    await _halt_in_memory(sdk_with_presence.presence, agent_id)

    client = _FakeAsyncAnthropicClient()
    wrapped = wrap_anthropic(client, sdk=sdk_with_presence, agent_id=agent_id)  # type: ignore[arg-type]

    with pytest.raises(AgentHaltedError):
        await wrapped.messages.create(
            model="claude-sonnet-4-6", max_tokens=32, messages=[],
        )


@pytest.mark.asyncio
async def test_wrap_anthropic_passes_when_not_halted(
    sdk_with_presence: FakeSDK,
) -> None:
    agent_id = "live-anthropic-agent"
    await sdk_with_presence.presence.heartbeat(agent_id)
    await sdk_with_presence.presence.force_refresh_halted_cache()

    client = _FakeAsyncAnthropicClient()
    wrapped = wrap_anthropic(client, sdk=sdk_with_presence, agent_id=agent_id)  # type: ignore[arg-type]

    result = await wrapped.messages.create(
        model="claude-sonnet-4-6", max_tokens=32, messages=[],
    )
    assert result is not None
