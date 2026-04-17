"""v0.6.2 P0 Bug #6: verify_session_chain must chunk + offload HMAC work.

Pre-fix, the endpoint issued one unbounded SELECT, materialized every
row into Python, then HMAC'd each one on the event loop. A 600k-event
session blocked the worker for seconds — every other request queued
behind it.

Fix: keyset-paginate the SELECT in 10k-row chunks, and hand the
synchronous HMAC work for each chunk to ``asyncio.to_thread`` so the
event loop keeps serving. External semantics are preserved: one
``verified`` verdict, one ``events`` list, same ``first_failure``
contract.

Two things we verify here:
  1. Correctness: a large session (30k rows, legitimate HMACs) still
     returns verified=True with event_count=30000.
  2. Loop responsiveness: while a verify is in-flight, another async
     task can still run. Pre-fix, the HMAC loop would monopolize the
     event loop until completion; post-fix, ``asyncio.to_thread``
     releases it during each chunk's thread execution.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest


def _hmac_row(
    *,
    secret: bytes,
    event_id: UUID,
    session_id: UUID,
    agent_id: str,
    parent_event_id: UUID | None,
    kind: str,
    metadata: dict[str, Any],
    prev_hash: str | None,
    created_at: datetime,
) -> str:
    from codeatelier_governance.audit.chain import compute_event_hmac

    return compute_event_hmac(
        secret=secret,
        event_id=event_id,
        session_id=session_id,
        agent_id=agent_id,
        parent_event_id=parent_event_id,
        kind=kind,
        input_hash=None,
        output_hash=None,
        metadata=metadata,
        prev_hash=prev_hash,
        created_at=created_at,
    )


def _build_chunked_engine_mock(
    all_rows: list[dict[str, Any]],
) -> Any:
    """Mock engine emulating keyset-paginated chain SELECT by chain_seq."""

    async def mock_execute(stmt: Any, params: Any = None) -> Any:
        params = params or {}
        result = MagicMock()
        sql = str(stmt.text) if hasattr(stmt, "text") else str(stmt)

        if "governance_audit_chain_keys" in sql:
            result.mappings.return_value = []
            return result

        if "governance_audit_events" in sql and "session_id" in sql:
            last_seq = int(params.get("last_seq", -1))
            chunk = int(params.get("chunk", 10000))
            # Return rows strictly greater than last_seq, ordered by seq.
            matched = [
                r for r in all_rows if int(r["chain_seq"]) > last_seq
            ]
            matched.sort(key=lambda r: int(r["chain_seq"]))
            result.mappings.return_value = matched[:chunk]
            return result

        result.mappings.return_value = []
        return result

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=mock_execute)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_ctx)
    return mock_engine


def _make_session_rows(
    session_id: UUID, secret: bytes, count: int
) -> list[dict[str, Any]]:
    """Build ``count`` well-formed chain rows, each with a real HMAC."""
    created_at = datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    for i in range(count):
        eid = uuid4()
        mac = _hmac_row(
            secret=secret,
            event_id=eid,
            session_id=session_id,
            agent_id="agent-x",
            parent_event_id=None,
            kind="scope.ok",
            metadata={},
            prev_hash=None,
            created_at=created_at,
        )
        rows.append(
            {
                "chain_seq": i + 1,
                "event_id": eid,
                "session_id": session_id,
                "agent_id": "agent-x",
                "parent_event_id": None,
                "kind": "scope.ok",
                "model": None,
                "input_hash": None,
                "output_hash": None,
                "metadata_json": {},
                "prev_hash": None,
                "hmac_value": mac,
                "hmac_next": None,
                "created_at": created_at,
            }
        )
    return rows


@pytest.mark.asyncio
async def test_large_session_verifies_across_chunks() -> None:
    """30k legitimate events → verified=True, event_count=30000.

    This exercises the chunk boundary (CHUNK_SIZE=10000 → 3 chunks)
    and asserts the chunked path still produces a correct verdict.
    Pre-fix the endpoint also produced this verdict, but by loading
    all rows into memory at once — we need to guarantee the new
    keyset loop walks every chunk.
    """
    from codeatelier_governance.console import app as console_app

    secret = b"x" * 32
    session_id = uuid4()
    rows = _make_session_rows(session_id, secret, count=30000)
    engine = _build_chunked_engine_mock(rows)

    with patch.object(console_app, "AUDIT_SECRET", "x" * 32), patch.object(
        console_app, "engine", engine
    ):
        result = await console_app.verify_session_chain(session_id)

    assert result["event_count"] == 30000, (
        f"chunked path dropped rows: got {result['event_count']}/30000"
    )
    assert result["verified"] is True
    assert len(result["events"]) == 30000


@pytest.mark.asyncio
async def test_large_session_does_not_starve_event_loop() -> None:
    """A parallel task MUST make progress while the verify runs.

    If the HMAC work stayed on the event loop (pre-fix), the parallel
    ticker below would not tick until the verify returned. Post-fix,
    ``asyncio.to_thread`` releases the loop during each chunk's
    thread execution — the ticker should tick many times.
    """
    from codeatelier_governance.console import app as console_app

    secret = b"x" * 32
    session_id = uuid4()
    # 30k rows is enough HMAC work that, if ran on the loop, the
    # ticker below would barely advance. Keep it modest so the test
    # stays fast when the fix is in.
    rows = _make_session_rows(session_id, secret, count=30000)
    engine = _build_chunked_engine_mock(rows)

    tick_count = 0
    stop = False

    async def ticker() -> None:
        nonlocal tick_count
        while not stop:
            tick_count += 1
            await asyncio.sleep(0)  # yield to the loop

    with patch.object(console_app, "AUDIT_SECRET", "x" * 32), patch.object(
        console_app, "engine", engine
    ):
        task = asyncio.create_task(ticker())
        try:
            result = await console_app.verify_session_chain(session_id)
        finally:
            stop = True
            await task

    assert result["verified"] is True
    # A loop that's been yielded to during thread-bound HMAC work
    # should have ticked many times — well over 10. Pre-fix the
    # ticker might tick 0-1 times because the HMAC loop monopolized
    # the event loop end-to-end.
    assert tick_count > 10, (
        f"event loop appears starved during verify: ticker only ticked "
        f"{tick_count} times — asyncio.to_thread offload missing"
    )


@pytest.mark.asyncio
async def test_large_session_tamper_detected_across_chunks() -> None:
    """Tamper on the LAST chunk must still flip verified=False.

    Pre-fix the loop materialized everything and couldn't miss this.
    Post-fix we must guarantee every chunk flows through the verifier
    (not short-circuited after chunk 1).
    """
    from codeatelier_governance.console import app as console_app

    secret = b"x" * 32
    session_id = uuid4()
    rows = _make_session_rows(session_id, secret, count=25000)
    # Tamper with hmac_value on row 22_500 (deep in chunk 3). Flip the
    # first nibble so the tamper is guaranteed to differ from the real
    # MAC regardless of its leading byte.
    tampered = rows[22_500]
    original = tampered["hmac_value"]
    flipped = ("f" if original[0] != "f" else "0") + original[1:]
    tampered["hmac_value"] = flipped
    assert tampered["hmac_value"] != original

    engine = _build_chunked_engine_mock(rows)

    with patch.object(console_app, "AUDIT_SECRET", "x" * 32), patch.object(
        console_app, "engine", engine
    ):
        result = await console_app.verify_session_chain(session_id)

    assert result["verified"] is False, (
        "tamper deep inside a late chunk was not detected — the chunked "
        "verifier is dropping rows after the first chunk"
    )
    assert result["first_failure"] == str(tampered["event_id"])
    assert result["event_count"] == 25000
