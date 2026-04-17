"""v0.6.2 P0 Bug #5: /api/gates/pending must be bounded + paginated.

Pre-fix, ``GET /api/gates/pending`` issued an unbounded SELECT:
``ORDER BY created_at DESC`` with no LIMIT. A reviewer going OOO plus
50k queued rows meant the browser would fetch the entire table in
one shot and OOM on render. A first-year SRE attack — or a simple
organic backlog — would take the console down.

Fix: default LIMIT=500 (max 1000), expose a ``limit`` query param, and
return ``has_more`` + ``next_cursor`` so the client can paginate. The
ordering is ``(created_at DESC, request_id)`` to make the cursor
deterministic across ties.

These tests drive the endpoint handler directly (no HTTP layer) with
an in-memory mock that simulates keyset pagination against the SELECT
the endpoint issues. Pre-fix, the handler would not accept ``limit``
as a param (would 500 / TypeError) and would emit a flat list with no
pagination signal — both assertions lock the fix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _seed_rows(count: int) -> list[dict[str, Any]]:
    """Deterministic pending-gate rows, ordered newest-first."""
    base = datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    for i in range(count):
        rows.append(
            {
                # Newest first: larger i → later created_at.
                "request_id": uuid4(),
                "agent_id": f"agent-{i % 5}",
                "kind": "net.fetch",
                "action_hash": f"sha256:{i:064x}",
                "created_at": base + timedelta(seconds=i),
                "expires_at": base + timedelta(hours=24, seconds=i),
                "reviewer_id": None,
                "reviewing_since": None,
            }
        )
    # Endpoint returns ORDER BY created_at DESC, request_id.
    rows.sort(key=lambda r: (r["created_at"], str(r["request_id"])), reverse=True)
    # Secondary sort (request_id) must be ASCENDING when created_at ties.
    # Re-apply the tiebreaker respecting the DESC-then-ASC rule.
    rows.sort(key=lambda r: (-r["created_at"].timestamp(), str(r["request_id"])))
    return rows


def _build_engine_mock(all_rows: list[dict[str, Any]]) -> Any:
    """Mock engine: applies LIMIT + keyset WHERE clause the endpoint uses."""

    async def mock_execute(stmt: Any, params: Any = None) -> Any:
        params = params or {}
        result = MagicMock()

        # The endpoint emits the same SELECT with an optional keyset
        # WHERE. Respect ``lim`` and the cursor params to replay over
        # the seeded dataset.
        filtered = list(all_rows)
        c_ts = params.get("c_ts")
        c_rid = params.get("c_rid")
        if c_ts is not None and c_rid is not None:
            filtered = [
                r
                for r in all_rows
                if r["created_at"] < c_ts
                or (r["created_at"] == c_ts and str(r["request_id"]) > c_rid)
            ]
        lim = int(params.get("lim", 500))
        result.mappings.return_value = filtered[:lim]
        return result

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=mock_execute)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_ctx)
    return mock_engine


@pytest.mark.asyncio
async def test_pending_bounded_at_default_limit_with_more_available() -> None:
    """600 pending rows → 500 returned, has_more=True, cursor present.

    Pre-fix there was no LIMIT and no has_more / next_cursor, so this
    test locks both the cap and the pagination signal.
    """
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(600)
    engine = _build_engine_mock(rows)

    with patch.object(console_app, "engine", engine):
        result = await console_app.gates_pending_v2()

    assert isinstance(result, dict), (
        "gates_pending must return a dict with items/has_more/next_cursor; "
        "pre-fix it returned a raw list of 600 items (browser OOM risk)"
    )
    assert len(result["items"]) == 500, (
        f"default LIMIT should be 500, got {len(result['items'])} — "
        "unbounded SELECT not fixed"
    )
    assert result["has_more"] is True
    assert result["next_cursor"] is not None
    assert "|" in result["next_cursor"], "cursor must encode (ts|request_id)"


@pytest.mark.asyncio
async def test_pending_cursor_walks_the_rest() -> None:
    """Fetching with next_cursor returns the remaining 100 rows."""
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(600)
    engine = _build_engine_mock(rows)

    with patch.object(console_app, "engine", engine):
        first = await console_app.gates_pending_v2()
        assert first["has_more"] is True
        second = await console_app.gates_pending_v2(cursor=first["next_cursor"])

    assert len(second["items"]) == 100, (
        f"second page should return the remaining 100 rows, got "
        f"{len(second['items'])}"
    )
    assert second["has_more"] is False
    assert second["next_cursor"] is None

    # No overlap between pages: request_id sets are disjoint.
    first_ids = {it["request_id"] for it in first["items"]}
    second_ids = {it["request_id"] for it in second["items"]}
    assert first_ids.isdisjoint(second_ids), (
        "cursor pagination is re-reading rows — keyset WHERE clause is wrong"
    )
    assert len(first_ids) + len(second_ids) == 600


@pytest.mark.asyncio
async def test_pending_custom_limit_respected_and_capped() -> None:
    """limit=50 returns 50 rows; limit=1000 returns the full 600 set."""
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(600)
    engine = _build_engine_mock(rows)

    with patch.object(console_app, "engine", engine):
        small = await console_app.gates_pending_v2(limit=50)
    assert len(small["items"]) == 50
    assert small["has_more"] is True

    # When calling the handler directly, FastAPI's Query(..., le=1000)
    # default is applied by FastAPI only on the HTTP path. Sanity-check
    # the endpoint still works at the documented upper bound.
    with patch.object(console_app, "engine", engine):
        big = await console_app.gates_pending_v2(limit=1000)
    # dataset is only 600 rows, so limit=1000 → everything, no more.
    assert len(big["items"]) == 600
    assert big["has_more"] is False
    assert big["next_cursor"] is None


@pytest.mark.asyncio
async def test_pending_empty_table() -> None:
    """No pending rows → empty items, has_more=False, no cursor."""
    from codeatelier_governance.console import app as console_app

    engine = _build_engine_mock([])

    with patch.object(console_app, "engine", engine):
        result = await console_app.gates_pending_v2()

    assert result["items"] == []
    assert result["has_more"] is False
    assert result["next_cursor"] is None
