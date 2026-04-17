"""v0.6.2 followup upgrade-breaker: /api/gates/pending shape versioning.

v0.6.2 silently changed ``/api/gates/pending`` from
``list[GatePending]`` to ``{items, has_more, next_cursor}``. External
Python callers (CI gate-checkers, cron scripts) broke — a ``for g in
pending:`` that used to iterate gate rows now iterates dict keys
("items", "has_more", "next_cursor"), silently skipping the queue.

Fix:
    * ``GET /api/gates/pending`` returns the legacy list shape again,
      capped hard at 500 rows, with ``Deprecation: true`` and
      ``Sunset`` + ``Link: rel=successor-version`` headers.
    * ``GET /api/v2/gates/pending`` returns the new paged dict.
    * When the legacy route hits its 500-row cap, it adds
      ``X-Truncated: true`` + ``X-Total-Returned`` so callers that
      can't read ``has_more`` still have a signal.
    * A structlog WARN fires once per worker process the first time
      the legacy route serves a response.

These tests drive the handlers directly (no full HTTP layer) so the
role check + auth dependencies don't need stubbing. For the header
assertions we unwrap the Starlette JSONResponse.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _seed_rows(count: int) -> list[dict[str, Any]]:
    base = datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    for i in range(count):
        rows.append(
            {
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
    rows.sort(key=lambda r: (-r["created_at"].timestamp(), str(r["request_id"])))
    return rows


def _build_engine_mock(all_rows: list[dict[str, Any]]) -> Any:
    async def mock_execute(stmt: Any, params: Any = None) -> Any:
        params = params or {}
        result = MagicMock()
        lim = int(params.get("lim", 500))
        result.mappings.return_value = all_rows[:lim]
        return result

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=mock_execute)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_ctx)
    return mock_engine


def _decode_json_response(resp: Any) -> Any:
    """Extract the Python object from a FastAPI/Starlette JSONResponse."""
    import json as _json
    return _json.loads(bytes(resp.body).decode("utf-8"))


@pytest.mark.asyncio
async def test_v1_returns_plain_list_shape() -> None:
    """Legacy route returns list, not dict. Unbreaks external API consumers.

    Pre-fix, v0.6.2 returned {items, has_more, next_cursor} on the
    same URL, so `for g in pending` silently iterated dict keys.
    """
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(10)
    engine = _build_engine_mock(rows)

    with patch.object(console_app, "engine", engine):
        resp = await console_app.gates_pending()

    payload = _decode_json_response(resp)
    assert isinstance(payload, list), (
        f"legacy /api/gates/pending must return list, got {type(payload).__name__} "
        f"— external consumers will break silently again"
    )
    assert len(payload) == 10
    assert all("request_id" in r for r in payload)


@pytest.mark.asyncio
async def test_v1_has_deprecation_headers() -> None:
    """Legacy route advertises deprecation + successor-version link."""
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(5)
    engine = _build_engine_mock(rows)

    with patch.object(console_app, "engine", engine):
        resp = await console_app.gates_pending()

    assert resp.headers["Deprecation"] == "true", (
        "v1 route must emit Deprecation: true header"
    )
    assert "Sunset" in resp.headers
    link = resp.headers["Link"]
    assert "/api/v2/gates/pending" in link
    assert 'rel="successor-version"' in link


@pytest.mark.asyncio
async def test_v1_x_truncated_header_when_cap_hit() -> None:
    """When legacy route hits its 500 cap, X-Truncated=true fires.

    Without this signal, legacy consumers (who can't see has_more)
    would silently miss anything past row 500.
    """
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(600)
    engine = _build_engine_mock(rows)

    with patch.object(console_app, "engine", engine):
        resp = await console_app.gates_pending()

    payload = _decode_json_response(resp)
    assert len(payload) == 500
    assert resp.headers.get("X-Truncated") == "true"
    assert resp.headers.get("X-Total-Returned") == "500"


@pytest.mark.asyncio
async def test_v1_no_x_truncated_when_under_cap() -> None:
    """If the full queue fits under 500, no X-Truncated is set."""
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(42)
    engine = _build_engine_mock(rows)

    with patch.object(console_app, "engine", engine):
        resp = await console_app.gates_pending()

    payload = _decode_json_response(resp)
    assert len(payload) == 42
    assert "X-Truncated" not in resp.headers


@pytest.mark.asyncio
async def test_v2_returns_paged_dict_shape() -> None:
    """v2 route returns {items, has_more, next_cursor} — new paged shape."""
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(10)
    engine = _build_engine_mock(rows)

    with patch.object(console_app, "engine", engine):
        result = await console_app.gates_pending_v2()

    assert isinstance(result, dict)
    assert set(result.keys()) == {"items", "has_more", "next_cursor"}
    assert len(result["items"]) == 10
    assert result["has_more"] is False
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_v1_and_v2_share_underlying_query() -> None:
    """v1 items equal the first 500 of v2's items for the same inputs.

    Regression lock: v1 must not drift from v2's SELECT (different
    WHERE clause, different ORDER BY) because compliance reports
    compare counts across both routes.
    """
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(200)
    engine = _build_engine_mock(rows)

    with patch.object(console_app, "engine", engine):
        v1_resp = await console_app.gates_pending()
        v2 = await console_app.gates_pending_v2()

    v1_payload = _decode_json_response(v1_resp)
    v1_ids = [r["request_id"] for r in v1_payload]
    v2_ids = [r["request_id"] for r in v2["items"]]
    assert v1_ids == v2_ids, (
        "v1 and v2 must return the same rows in the same order "
        "(different WHERE/ORDER BY would fragment compliance reports)"
    )


@pytest.mark.asyncio
async def test_v1_legacy_warn_fires_once_per_process() -> None:
    """First legacy hit emits structlog warn; subsequent hits do NOT.

    Operator signal: loud-but-bounded. If every hit emitted, a single
    legacy cron would bury the log. If never emitted, operators miss
    the migration signal.
    """
    from codeatelier_governance.console import app as console_app

    rows = _seed_rows(5)
    engine = _build_engine_mock(rows)

    # Reset the module-level guard between test runs (tests share a
    # process in pytest).
    console_app._GATES_PENDING_LEGACY_WARNED = False

    warn_calls: list[tuple[str, dict[str, Any]]] = []

    def _capture(event: str, **kwargs: Any) -> None:
        warn_calls.append((event, kwargs))

    with patch.object(console_app, "engine", engine):
        with patch.object(console_app._logger, "warning", side_effect=_capture):
            await console_app.gates_pending()
            await console_app.gates_pending()
            await console_app.gates_pending()

    deprecation_warns = [
        c for c in warn_calls
        if c[0] == "console.gates_pending_deprecated_shape_served"
    ]
    assert len(deprecation_warns) == 1, (
        f"legacy-shape warn must fire exactly once per process; got "
        f"{len(deprecation_warns)}"
    )
