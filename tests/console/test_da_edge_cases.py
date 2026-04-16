"""DA edge-case findings batch for console surface (F2 P0 + F3).

Covers:
    #1  GET /api/events/{event_id} with non-UUID → client error (not 500)
    #19 redact_secrets on nested dict does not mutate input
    #20 sk-ant-* matches anthropic pattern, not openai
    #21 BatchApproveResponse partial failure list
    #22 SessionRevokeResponse shape when audit logger raises
    #23 GateContextResponse accepts nested presence + recent events
    #27 /health/governance unauthenticated → exactly {"status":"ok"}
"""
from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from codeatelier_governance.console import app as app_module
from codeatelier_governance.console.models.responses import (
    BatchApproveFailure,
    BatchApproveResponse,
    GateAgentPresence,
    GateContextResponse,
    GateRecentEvent,
    SessionRevokeResponse,
)
from codeatelier_governance.console.redaction import redact_secrets


# ---------------------------------------------------------------------------
# Test #1 — GET /api/events/{event_id} with non-UUID
# ---------------------------------------------------------------------------
def test_get_audit_event_id_not_uuid_returns_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-UUID event id must not crash or 500. The endpoint parses it
    itself and must return a 4xx (400 in current impl; 422 would also be
    acceptable were FastAPI validation wired to ``UUID`` path type)."""
    monkeypatch.setenv("CONSOLE_DEV_MODE", "1")
    monkeypatch.setattr(app_module, "DEV_MODE", True)
    # Non-None engine satisfies the 503 guard. The code path returns 400
    # before touching it when the UUID parse fails.
    monkeypatch.setattr(app_module, "engine", object())
    client = TestClient(app_module.app)
    resp = client.get("/api/events/not-a-uuid")
    assert resp.status_code != 500, resp.text
    assert 400 <= resp.status_code < 500


# ---------------------------------------------------------------------------
# Test #19 — redact_secrets non-mutating on nested dict
# ---------------------------------------------------------------------------
def test_redact_secrets_nested_dict_does_not_mutate_input() -> None:
    original = {
        "note": "my key: sk-ant-abcdefghijklmnopqrstuv",
        "inner": {
            "list": ["ghp_abcdefghijklmnopqrstuvwxyz0123456789", 1],
            "plain": "ok",
        },
    }
    snapshot = copy.deepcopy(original)
    out = redact_secrets(original)
    assert original == snapshot, "redact_secrets mutated its input"
    # And the output is redacted.
    assert "[REDACTED]" in out["note"]
    assert out["inner"]["list"][0] == "[REDACTED]"


# ---------------------------------------------------------------------------
# Test #20 — sk-ant- matches anthropic pattern, not openai
# ---------------------------------------------------------------------------
def test_sk_ant_matches_anthropic_pattern_not_openai() -> None:
    """A single sk-ant- token must produce exactly one [REDACTED] span,
    not two (which would indicate the openai pattern fired on the
    already-redacted output)."""
    token = "sk-ant-xxxxxxxxxxxxxxxxxxxx"
    blob = f"before {token} after"
    out = redact_secrets(blob)
    # Exactly one redaction happened.
    assert out.count("[REDACTED]") == 1
    # Original token gone.
    assert token not in out
    # Nothing else was mangled.
    assert out.startswith("before ")
    assert out.endswith(" after")


# ---------------------------------------------------------------------------
# Test #21 — BatchApproveResponse partial failure shape
# ---------------------------------------------------------------------------
def test_batch_approve_partial_failure_returns_failure_list() -> None:
    """Build the response directly; we're pinning the SHAPE (strict
    StrictResponse forbids extras) and that BatchApproveFailure rows
    are a valid item type inside it."""
    resp = BatchApproveResponse(
        ok=True,
        approved=["req-a", "req-b"],
        failed=[
            BatchApproveFailure(request_id="req-c", reason="already_resolved"),
        ],
        approved_count=2,
        failed_count=1,
    )
    assert resp.ok is True
    assert len(resp.approved) == 2
    assert len(resp.failed) == 1
    assert resp.failed[0].reason == "already_resolved"


# ---------------------------------------------------------------------------
# Test #22 — SessionRevokeResponse still returned when audit log raises
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_session_revoke_audit_failure_does_not_break_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the audit writer raises, revoke_session must still complete.

    Invariant: observation (audit) failures must never break the user's
    control-plane call.
    """
    sid = uuid4()

    class _FakeRow(dict):  # type: ignore[type-arg]
        def __getitem__(self, k: Any) -> Any:  # type: ignore[override]
            if isinstance(k, int):
                return list(self.values())[k]
            return super().__getitem__(k)

    class _FakeResult:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = [_FakeRow(r) for r in rows]
            self.rowcount = len(self._rows)

        def mappings(self) -> "_FakeResult":
            return self

        def first(self) -> Any:
            return self._rows[0] if self._rows else None

    class _FakeConn:
        async def execute(self, *_args: Any, **_kw: Any) -> _FakeResult:
            return _FakeResult(
                [{"created_at": datetime.now(timezone.utc)}]
            )

    class _FakeEngine:
        @asynccontextmanager
        async def begin(self) -> Any:
            yield _FakeConn()

        @asynccontextmanager
        async def connect(self) -> Any:
            yield _FakeConn()

    class _BrokenAudit:
        async def log(self, _event: Any) -> None:
            raise RuntimeError("audit down")

    monkeypatch.setattr(app_module, "engine", _FakeEngine())
    monkeypatch.setattr(app_module, "audit_module", _BrokenAudit())
    monkeypatch.setattr(app_module, "_revoke_sse_session", lambda _sid: None)

    request = MagicMock()
    request.state.user_id = "operator-1"
    # The handler may or may not swallow the audit failure; assert the
    # observable property: either it returns a response OR it raises a
    # specific audit error. In either case it must NOT leak a raw
    # RuntimeError as a 500 on the happy path.
    try:
        result = await app_module.revoke_session(sid, request)
    except RuntimeError:
        pytest.fail(
            "audit.log RuntimeError leaked into control-plane path — "
            "audit failure must not break revoke_session"
        )
    assert isinstance(result, SessionRevokeResponse)
    assert result.ok is True
    assert result.session_id == str(sid)


# ---------------------------------------------------------------------------
# Test #23 — GateContextResponse accepts nested presence + recent events
# ---------------------------------------------------------------------------
def test_gate_context_includes_presence_and_recent_events() -> None:
    resp = GateContextResponse(
        request_id="req-1",
        agent_id="a1",
        kind="tool.call",
        risk="LOW",
        agent_presence=GateAgentPresence(
            status="live",
            last_heartbeat=datetime.now(timezone.utc),
        ),
        recent_agent_events=[
            GateRecentEvent(kind="tool.call"),
            GateRecentEvent(kind="tool.result"),
        ],
        payload={},
    )
    assert resp.agent_presence is not None
    assert resp.agent_presence.status == "live"
    assert len(resp.recent_agent_events) == 2


# ---------------------------------------------------------------------------
# Test #27 — /health/governance anonymous must return only {"status":"ok"}
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_health_unauthenticated_returns_only_status_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Call the governance_health handler directly with a Request that
    does NOT look authenticated. Response must be exactly {"status":"ok"} —
    no extra fields, no governance internals."""
    # Force the anonymous path by nulling engine so the authenticated
    # DB probe returns a minimal shape, AND by ensuring _health_authenticated
    # returns False for a bare MagicMock.
    monkeypatch.setattr(app_module, "engine", None)

    async def _not_authed(_req: Any) -> bool:
        return False

    monkeypatch.setattr(app_module, "_health_authenticated", _not_authed)
    request = MagicMock()
    result = await app_module.governance_health(request)
    assert result == {"status": "ok"}
