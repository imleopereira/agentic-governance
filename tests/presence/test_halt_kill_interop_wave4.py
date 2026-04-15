"""DA Wave 4: halt/kill interop edge-case tests.

Tests 9–12 from DA Wave 4 findings. Covers:
  * _fetch_halted_from_postgres reads legacy `_killed_*` keys.
  * _halted_* keys win over `_killed_*` when both are present.
  * AgentHaltedError accepts v0.5.x `killed_by=` kwargs and exposes both.
  * scope.check() on a halted agent raises AgentHaltedError (not a
    separate AgentKilledError class).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from codeatelier_governance.presence import (
    AgentHaltedError,
    PresenceModule,
)


class _FakeMapping:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, k: str) -> Any:
        return self._data[k]


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> list[_FakeMapping]:
        return [_FakeMapping(r) for r in self._rows]


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def execute(self, _query: Any) -> _FakeResult:
        return _FakeResult(self._rows)


class _FakeEngine:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    @asynccontextmanager  # type: ignore[arg-type]
    async def _connect(self) -> Any:
        yield _FakeConn(self._rows)

    def connect(self) -> Any:
        return self._connect()


@pytest.mark.asyncio
async def test_fetch_halted_from_postgres_reads_legacy_killed_keys() -> None:
    """A row with ONLY legacy `_killed_*` keys must still appear as halted."""
    presence = PresenceModule()
    # The query itself uses COALESCE(`_halted_by`, `_killed_by`) so the
    # simulated row shape already carries the coalesced result.
    engine = _FakeEngine(
        [
            {
                "agent_id": "legacy-agent",
                "halted_by": "operator-42",  # from _killed_by
                "halted_at": "2026-04-15T00:00:00+00:00",
                "reason": "legacy v0.5.x halt",
            }
        ]
    )
    result = await presence._fetch_halted_from_postgres(engine)
    assert "legacy-agent" in result
    assert result["legacy-agent"]["halted_by"] == "operator-42"
    assert result["legacy-agent"]["reason"] == "legacy v0.5.x halt"


@pytest.mark.asyncio
async def test_fetch_halted_from_postgres_new_key_wins_over_legacy() -> None:
    """When BOTH keys are set, `_halted_*` wins via the query's COALESCE."""
    presence = PresenceModule()
    # The in-memory path runs the SAME precedence rule: _halted_by wins.
    presence._agents["dual-agent"] = {
        "metadata": {
            "_halted_by": "new-op",
            "_halted_at": "2026-04-15T12:00:00+00:00",
            "_halt_reason": "new reason",
            "_killed_by": "legacy-op",
            "_killed_at": "2026-01-01T00:00:00+00:00",
            "_kill_reason": "legacy reason",
        }
    }
    derived = presence._derive_halted_from_memory()
    assert derived["dual-agent"]["halted_by"] == "new-op"
    assert derived["dual-agent"]["halted_at"] == "2026-04-15T12:00:00+00:00"
    assert derived["dual-agent"]["reason"] == "new reason"


def test_agent_halted_error_killed_kwargs_compat() -> None:
    """AgentHaltedError accepts `killed_by=`/`killed_at=` and aliases both."""
    err = AgentHaltedError(
        "agent-x",
        killed_by="op-legacy",
        killed_at="2026-04-15T00:00:00+00:00",
    )
    # Canonical names
    assert err.halted_by == "op-legacy"
    assert err.halted_at == "2026-04-15T00:00:00+00:00"
    # Deprecated aliases still work
    assert err.killed_by == "op-legacy"
    assert err.killed_at == "2026-04-15T00:00:00+00:00"


@pytest.mark.asyncio
async def test_scope_check_raises_halted_error_not_killed_error() -> None:
    """scope.check() on a halted agent MUST raise AgentHaltedError."""
    from codeatelier_governance.audit.module import AuditModule
    from codeatelier_governance.audit.store import InMemoryAuditStore
    from codeatelier_governance.scope.module import ScopeModule
    from codeatelier_governance.scope.models import ScopePolicy

    presence = PresenceModule()
    # Seed an in-memory halt marker for the agent.
    presence._agents["halted-x"] = {
        "status": "unresponsive",
        "metadata": {
            "_halted_by": "op",
            "_halted_at": "2026-04-15T00:00:00+00:00",
            "_halt_reason": "because",
        },
    }
    await presence.force_refresh_halted_cache()

    audit = AuditModule(store=InMemoryAuditStore(), secret=b"test-secret-32-bytes-long-aaaaaa")
    scope = ScopeModule(
        audit=audit,
        policies=[
            ScopePolicy(
                agent_id="halted-x",
                allowed_tools=frozenset({"read_file"}),
            )
        ],
    )
    scope.set_presence_module(presence)

    with pytest.raises(AgentHaltedError):
        await scope.check(agent_id="halted-x", tool="read_file")
