"""DA Wave 4: halt/kill interop edge-case tests.

Tests 9–12 from DA Wave 4 findings, updated for the v0.7 columns-only halt
marker. Covers:
  * _fetch_halted_from_postgres reads the dedicated `halted_by` column.
  * Legacy `_halted_*` / `_killed_*` metadata markers are NOT honored (v0.7
    stopped reading them; a7f2haltcols backfilled real ones into columns).
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
async def test_fetch_halted_from_postgres_reads_halted_by_column() -> None:
    """A row whose dedicated `halted_by` column is set appears as halted."""
    presence = PresenceModule()
    # v0.7: the query selects the dedicated columns directly (no metadata
    # COALESCE). The fake row carries the column values the query returns.
    engine = _FakeEngine(
        [
            {
                "agent_id": "legacy-agent",
                "halted_by": "operator-42",
                "halted_at": "2026-04-15T00:00:00+00:00",
                "reason": "backfilled from a pre-v0.7 marker",
            }
        ]
    )
    result = await presence._fetch_halted_from_postgres(engine)
    assert "legacy-agent" in result
    assert result["legacy-agent"]["halted_by"] == "operator-42"
    assert result["legacy-agent"]["reason"] == "backfilled from a pre-v0.7 marker"


@pytest.mark.asyncio
async def test_derive_halted_from_memory_ignores_metadata_markers() -> None:
    """v0.7: metadata `_halted_*` / `_killed_*` markers are NOT honored.

    Only the dedicated top-level marker halts an agent. A row carrying the
    halt solely in `metadata` (which the agent's own heartbeat rewrites
    wholesale) must derive as NOT halted, closing the self-unhalt vector.
    """
    presence = PresenceModule()
    presence._agents["meta-only-agent"] = {
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
    assert "meta-only-agent" not in derived


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
    # Seed the dedicated top-level halt marker (v0.7: metadata markers are no
    # longer honored).
    presence._agents["halted-x"] = {
        "status": "unresponsive",
        "halted_by": "op",
        "halted_at": "2026-04-15T00:00:00+00:00",
        "halt_reason": "because",
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
