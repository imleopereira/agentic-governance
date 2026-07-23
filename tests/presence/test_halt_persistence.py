"""halt() persistence contract (blocker 1 fix).

A kill-switch write that fails or is denied MUST raise HaltPersistenceError,
never return a false success. And when a privileged ``halt_engine`` is
configured, the halt WRITE must go through it (not the shared app engine),
so the agent can run under a REVOKE'd role while the halt still persists.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from codeatelier_governance.presence import HaltPersistenceError, PresenceModule


class _FailingConn:
    async def execute(self, *a: Any, **k: Any) -> Any:
        # Mirrors the denial an app/agent role hits under the self-unhalt REVOKE.
        raise RuntimeError("permission denied for table governance_agent_presence")


class _FailingEngine:
    @asynccontextmanager
    async def begin(self) -> Any:
        yield _FailingConn()


class _Result:
    rowcount = 1


class _RecordingConn:
    def __init__(self, calls: list[Any]) -> None:
        self._calls = calls

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self._calls.append(params)
        return _Result()


class _RecordingEngine:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    @asynccontextmanager
    async def begin(self) -> Any:
        yield _RecordingConn(self.calls)


@pytest.mark.asyncio
async def test_halt_raises_when_write_denied() -> None:
    """A denied/failed halt write raises HaltPersistenceError, not a false success."""
    p = PresenceModule(engine=_FailingEngine())
    with pytest.raises(HaltPersistenceError):
        await p.halt("agent-1", halted_by="op", reason="incident")


@pytest.mark.asyncio
async def test_halt_uses_privileged_halt_engine_when_provided() -> None:
    """The halt write goes to the privileged halt_engine; the shared engine is untouched."""
    shared = _RecordingEngine()
    privileged = _RecordingEngine()
    p = PresenceModule(engine=shared, halt_engine=privileged)

    await p.halt("agent-1", halted_by="op", reason="incident")

    assert len(privileged.calls) == 1  # halt wrote via the privileged engine
    assert len(shared.calls) == 0      # shared (agent) engine never wrote the halt


@pytest.mark.asyncio
async def test_halt_falls_back_to_shared_engine_without_privileged() -> None:
    """With no halt_engine, halt() uses the shared engine (single-role deployment)."""
    shared = _RecordingEngine()
    p = PresenceModule(engine=shared)

    await p.halt("agent-1", halted_by="op", reason="incident")

    assert len(shared.calls) == 1
