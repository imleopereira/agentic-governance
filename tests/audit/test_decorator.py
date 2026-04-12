"""Tests for the @audit.track decorator."""
from __future__ import annotations

import pytest

from codeatelier_governance.audit import AuditModule


@pytest.mark.asyncio
async def test_track_logs_start_and_end(audit: AuditModule) -> None:
    @audit.track(kind="add", agent_id="math-agent")
    async def add(x: int, y: int) -> int:
        return x + y

    with audit.session() as sid:
        result = await add(2, 3)
    assert result == 5
    await audit._writer.flush()

    store = audit._store  # type: ignore[attr-defined]
    events = sorted(
        [e for e in store._events.values() if e.session_id == sid],  # type: ignore[attr-defined]
        key=lambda e: e.created_at,
    )
    kinds = [e.kind for e in events]
    assert kinds == ["add.start", "add.end"]
    assert events[0].input_hash is not None
    assert events[1].output_hash is not None
    # The end event is logged inside the parent_event(start) context, so it
    # links back to the start event for provenance.
    assert events[1].parent_event_id == events[0].event_id
    assert len(events[0].input_hash or "") == 64


@pytest.mark.asyncio
async def test_track_logs_error_and_reraises(audit: AuditModule) -> None:
    @audit.track(kind="boom", agent_id="x")
    async def explode() -> None:
        raise RuntimeError("nope")

    with audit.session() as sid:
        with pytest.raises(RuntimeError, match="nope"):
            await explode()
    await audit._writer.flush()

    store = audit._store  # type: ignore[attr-defined]
    events = sorted(
        [e for e in store._events.values() if e.session_id == sid],  # type: ignore[attr-defined]
        key=lambda e: e.created_at,
    )
    kinds = [e.kind for e in events]
    assert kinds == ["boom.start", "boom.error"]
    assert events[1].metadata.get("error_type") == "RuntimeError"


@pytest.mark.asyncio
async def test_track_rejects_sync_function(audit: AuditModule) -> None:
    with pytest.raises(TypeError, match="async function"):

        @audit.track(kind="bad")
        def sync_fn() -> None:  # pragma: no cover - decorator raises immediately
            pass


@pytest.mark.asyncio
async def test_track_capture_args_disabled(audit: AuditModule) -> None:
    @audit.track(kind="silent", agent_id="x", capture_args=False, capture_result=False)
    async def secret(token: str) -> str:
        return token + "!"

    with audit.session() as sid:
        await secret("password123")
    await audit._writer.flush()

    store = audit._store  # type: ignore[attr-defined]
    events = [e for e in store._events.values() if e.session_id == sid]  # type: ignore[attr-defined]
    for e in events:
        assert e.input_hash is None
        assert e.output_hash is None
