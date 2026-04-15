"""BLOCKER C2: tenant isolation on GET /api/events/{event_id}.

Pins:
  * Viewer cannot fetch an event for an agent outside their accessible
    list when ``GOVERNANCE_CONSOLE_SCOPE_VIEWERS_BY_AGENT=true``. Cross-
    tenant denial collapses to 404 to avoid event-id existence leakage.
  * Admin can fetch any event.
  * A successful fetch emits a ``pipeline.audit_event_fetched`` row to
    the audit chain.
  * The endpoint is wired with ``response_model=AuditEventView`` so the
    response is validated by the strict Pydantic model.
  * An unknown UUID returns 404, not 500.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from codeatelier_governance.console import app as app_module


def _make_row(
    *, event_id: str, agent_id: str = "agent-a", kind: str = "tool.call",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "chain_seq": 1,
        "agent_id": agent_id,
        "kind": kind,
        "model": None,
        "metadata_json": {},
        "hmac_value": "0" * 64,
        "prev_hash": None,
        "created_at": datetime.now(timezone.utc),
    }


def _patch_engine_with_row(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, Any] | None
) -> None:
    """Install a fake engine on app_module.engine that returns ``row``."""

    class FakeMappings:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self._row = row

        def first(self) -> Any:
            return self._row

    class FakeRes:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self._row = row

        def mappings(self) -> Any:
            return FakeMappings(self._row)

        def first(self) -> Any:
            return self._row

    class FakeConn:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self._row = row

        async def execute(self, *_a: Any, **_kw: Any) -> Any:
            return FakeRes(self._row)

    class FakeEngine:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self._row = row

        @asynccontextmanager
        async def connect(self) -> Any:
            yield FakeConn(self._row)

    monkeypatch.setattr(app_module, "engine", FakeEngine(row))


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(app_module, "DEV_MODE", True)
    monkeypatch.setattr(app_module, "audit_module", None)
    return TestClient(app_module.app)


def test_event_response_uses_pydantic_model(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    eid = str(uuid4())
    _patch_engine_with_row(monkeypatch, _make_row(event_id=eid))
    resp = admin_client.get(f"/api/events/{eid}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # AuditEventView exposes exactly these top-level fields.
    assert set(data.keys()) >= {
        "event_id", "chain_seq", "agent_id", "kind",
        "model", "tool", "request_id", "metadata",
        "hmac_value", "prev_hash", "created_at",
    }


def test_unknown_event_id_returns_404_not_500(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_engine_with_row(monkeypatch, None)
    resp = admin_client.get(f"/api/events/{uuid4()}")
    assert resp.status_code == 404


def test_admin_allowed_any_event(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    eid = str(uuid4())
    _patch_engine_with_row(monkeypatch, _make_row(event_id=eid, agent_id="agent-x"))
    monkeypatch.setenv("GOVERNANCE_CONSOLE_SCOPE_VIEWERS_BY_AGENT", "true")
    resp = admin_client.get(f"/api/events/{eid}")
    assert resp.status_code == 200


def test_viewer_denied_cross_tenant_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Viewer + scope-by-agent enabled → 404 (info-leak-safe).

    Calls the handler function directly so we sidestep FastAPI's
    dependency-injection re-analysis of the overridden authenticate.
    The handler is the same code path; only the auth context is faked
    here by hand-setting ``request.state``.
    """
    import asyncio
    from fastapi import HTTPException
    from unittest.mock import MagicMock

    monkeypatch.setattr(app_module, "audit_module", None)
    monkeypatch.setenv("GOVERNANCE_CONSOLE_SCOPE_VIEWERS_BY_AGENT", "true")
    eid = str(uuid4())
    _patch_engine_with_row(monkeypatch, _make_row(event_id=eid, agent_id="agent-b"))

    fake_request = MagicMock()
    fake_request.state.user_id = "viewer-1"
    fake_request.state.role = "viewer"
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(app_module.get_audit_event(eid, fake_request))
    assert excinfo.value.status_code == 404


def test_event_fetch_emits_audit_trail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful fetch must call audit_module.log with the trail event."""
    monkeypatch.setattr(app_module, "DEV_MODE", True)

    fake_audit = MagicMock()
    fake_audit.log = AsyncMock()
    monkeypatch.setattr(app_module, "audit_module", fake_audit)

    eid = str(uuid4())
    _patch_engine_with_row(monkeypatch, _make_row(event_id=eid))
    client = TestClient(app_module.app)
    resp = client.get(f"/api/events/{eid}")
    assert resp.status_code == 200, resp.text
    assert fake_audit.log.await_count == 1
    audit_event = fake_audit.log.await_args.args[0]
    assert audit_event.kind == "pipeline.audit_event_fetched"
    assert audit_event.metadata["fetched_event_id"] == eid
    assert "fetched_by" in audit_event.metadata
