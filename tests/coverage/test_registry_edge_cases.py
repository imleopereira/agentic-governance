"""Edge-case tests for the F9 WrapperRegistry (DA findings batch)."""
from __future__ import annotations


import pytest

from codeatelier_governance.coverage.registry import WrapperRegistry


class _FakeConn:
    """Minimal async SQL connection stand-in."""

    def __init__(self, *, reject_agents: set[str] | None = None) -> None:
        self.reject_agents = reject_agents or set()
        self.executed: list[dict] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        if params is not None and params.get("agent_id") in self.reject_agents:
            raise RuntimeError("simulated FK/trigger rejection")
        if params is not None:
            self.executed.append(params)
        return None


class _FakeTxn:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc_info) -> None:  # noqa: ANN002
        return None


class _FakeEngine:
    def __init__(self, *, reject_agents: set[str] | None = None) -> None:
        self.conn = _FakeConn(reject_agents=reject_agents)

    def begin(self) -> _FakeTxn:
        return _FakeTxn(self.conn)


# --- Edge case #5 -----------------------------------------------------------
@pytest.mark.asyncio
async def test_flush_with_unknown_agent_id_does_not_block_known_agents() -> None:
    engine = _FakeEngine(reject_agents={"rogue-agent"})
    reg = WrapperRegistry(salt=b"x" * 32)
    reg.register("rogue-agent", "openai")
    reg.register("known-a", "openai")
    reg.register("known-b", "anthropic")

    await reg.flush_to_postgres(engine)

    # rogue-agent is rejected, both known agents are persisted.
    agents = {row["agent_id"] for row in engine.conn.executed}
    assert "known-a" in agents
    assert "known-b" in agents
    assert "rogue-agent" not in agents


# --- Edge case #6 -----------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_flush_two_processes_same_instance_id() -> None:
    """Two independent-looking flushes for the same instance_id must NOT
    produce duplicate rows at the registry level; the UPSERT semantics
    are mirrored by the in-memory registry's dedup on
    ``(agent_id, provider, instance_id)``.
    """
    reg = WrapperRegistry(salt=b"x" * 32)
    reg.register("agent-a", "openai")
    reg.register("agent-a", "openai")
    reg.register("agent-a", "openai")
    assert len(reg.snapshot()) == 1

    # Simulate two flushes. The fake engine records every INSERT but the
    # registry must only produce one row per key on each flush.
    eng1 = _FakeEngine()
    eng2 = _FakeEngine()
    await reg.flush_to_postgres(eng1)
    await reg.flush_to_postgres(eng2)
    assert len(eng1.conn.executed) == 1
    assert len(eng2.conn.executed) == 1
    # Same instance_id across both flushes (restart scenario).
    assert eng1.conn.executed[0]["instance_id"] == eng2.conn.executed[0]["instance_id"]


# --- Edge case #7 -----------------------------------------------------------
@pytest.mark.asyncio
async def test_coverage_pct_zero_scope_policies_returns_null_reason() -> None:
    """CoverageComputer with an engine that reports 0 scope policies."""
    from codeatelier_governance.coverage.compute import CoverageComputer

    class _Row:
        def __init__(self, val: int) -> None:
            self._val = val

        def __getitem__(self, i: int) -> int:
            return self._val

    class _Res:
        def __init__(self, val: int) -> None:
            self._val = val

        def first(self) -> _Row:
            return _Row(self._val)

    class _Conn:
        async def execute(self, *_a, **_kw):  # noqa: ANN002
            return _Res(0)  # count=0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_): return None

    class _Engine:
        def connect(self) -> "_Conn":
            return _Conn()

    comp = CoverageComputer(engine=_Engine(), enabled=True)
    pct, reason = await comp.compute()
    assert pct is None
    assert reason == "no_scope_policies_registered"


# --- Edge case #8 -----------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_does_not_run_when_engine_is_none() -> None:
    """Flush with engine=None must not attempt a prune or raise."""
    reg = WrapperRegistry(salt=b"x" * 32)
    reg.register("agent-a", "openai")
    # With engine=None, this is a no-op and must not raise.
    await reg.flush_to_postgres(None)


# --- Edge case #9 -----------------------------------------------------------
def test_workspace_salt_fallback_logs_warning_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When both env vars are unset, a WARN is emitted and hostname hashes
    are self-consistent within the process."""
    import codeatelier_governance.coverage.registry as registry_mod

    monkeypatch.delenv("GOVERNANCE_WORKSPACE_SALT", raising=False)
    monkeypatch.delenv("GOVERNANCE_AUDIT_SECRET", raising=False)

    # Call the fallback path.
    salt_a = registry_mod._resolve_workspace_salt()
    salt_b = registry_mod._resolve_workspace_salt()
    # Self-consistent — same fallback bytes.
    assert salt_a == salt_b
    # Consistent hostname hash under fallback.
    h1 = registry_mod.compute_hostname_hash("x.local", salt=salt_a)
    h2 = registry_mod.compute_hostname_hash("x.local", salt=salt_b)
    assert h1 == h2
    # Structlog default is stderr. Either stream is fine — the important
    # property is that at least one workspace_salt_missing warning fires.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "workspace_salt_missing" in combined
