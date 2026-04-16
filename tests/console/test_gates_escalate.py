"""v0.6.1 S3 P1 #1 — escalate_gate claimant check.

Prior to v0.6.1, ``POST /api/gates/{request_id}/escalate`` let any
authenticated caller (including viewers) release another reviewer's
claim by NULL-ing ``reviewer_id``. A viewer could iterate the pending
queue and release every active claim in a loop, continuously griefing
the admin review workflow.

v0.6.1 adds a claimant check mirroring the one already present on
``grant_gate`` / ``deny_gate``:

* Unclaimed gate (reviewer_id IS NULL) → any authenticated user may
  escalate (preserves prior behavior so legit operators can kick off
  escalation on an abandoned queue).
* Claimed gate, caller IS the claimant → escalate proceeds.
* Claimed gate, caller IS admin → escalate proceeds.
* Claimed gate, caller is ANY other role → 403.

Also asserts the ``gates.escalated`` audit row is emitted with the
required metadata keys: ``reviewer_id_before``, ``reviewer_id_after``,
``by_user_id``, ``role``.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException


# ---------- Fixtures / helpers ------------------------------------------


def _make_mock_engine(gate_row: dict[str, Any]) -> Any:
    """Build a mock engine whose SELECT returns *gate_row*.

    The SELECT in ``escalate_gate`` pulls
    ``request_id, agent_id, kind, resolved_at, payload_json, reviewer_id``
    from ``governance_gates_pending``. The UPDATE uses
    ``RETURNING request_id`` and checks ``.first()`` for a row; the
    happy-path mock returns a truthy row there so the endpoint proceeds.
    The mock does NOT emulate the ``gate_change_notify`` trigger — we
    only care about the Python-side authz decision and the post-UPDATE
    audit row behaviour.
    """
    async def mock_execute(stmt: Any, params: Any = None) -> Any:
        result = MagicMock()
        sql_text = str(stmt.text) if hasattr(stmt, "text") else str(stmt)
        if "governance_gates_pending" in sql_text and "SELECT" in sql_text:
            result.mappings.return_value.first.return_value = gate_row
        elif "UPDATE" in sql_text:
            # The UPDATE returns a row so the happy-path endpoint
            # proceeds. Race-condition tests use their own mock that
            # returns None here.
            result.first.return_value = (str(params["rid"]),) if params else None
        else:
            result.mappings.return_value.first.return_value = None
        return result

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=mock_execute)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_ctx
    return mock_engine


def _make_request(user_id: str, role: str | None) -> Any:
    """Build a mock Request with request.state.user_id and role set."""
    request = MagicMock()
    request.state.user_id = user_id
    request.state.role = role
    return request


def _make_gate_row(
    request_id: UUID,
    *,
    reviewer_id: str | None,
    agent_id: str = "agent-alpha",
    kind: str = "HIGH_RISK",
) -> dict[str, Any]:
    return {
        "request_id": str(request_id),
        "agent_id": agent_id,
        "kind": kind,
        "resolved_at": None,
        "payload_json": {"risk": "HIGH"},
        "reviewer_id": reviewer_id,
    }


class _RecordingAuditModule:
    """Minimal stub that records ``log()`` calls."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def log(self, event: Any) -> None:
        self.events.append(event)


# ---------- Authorization matrix ----------------------------------------


class TestEscalateClaimantCheck:
    """Authz matrix for escalate_gate (v0.6.1 S3 P1 #1)."""

    def test_viewer_cannot_escalate_gate_claimed_by_another_reviewer(self) -> None:
        """A viewer calling escalate on someone else's claimed gate → 403."""
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        gate_row = _make_gate_row(
            request_id,
            reviewer_id="reviewer-alice",
        )
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="viewer-bob", role="viewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    escalate_gate(
                        request_id,
                        EscalateRequest(escalate_to="admin-on-call"),
                        request,
                    )
                )
        assert exc_info.value.status_code == 403
        assert "current reviewer or an admin" in str(exc_info.value.detail)

    def test_reviewer_role_cannot_escalate_others_claim(self) -> None:
        """A reviewer-role user cannot release ANOTHER reviewer's claim."""
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        gate_row = _make_gate_row(
            request_id,
            reviewer_id="reviewer-alice",
        )
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="reviewer-carol", role="reviewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    escalate_gate(
                        request_id,
                        EscalateRequest(escalate_to="admin-on-call"),
                        request,
                    )
                )
        assert exc_info.value.status_code == 403

    def test_reviewer_can_escalate_their_own_claim(self) -> None:
        """Reviewer releasing their own claim → success."""
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        gate_row = _make_gate_row(
            request_id,
            reviewer_id="reviewer-alice",
        )
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="reviewer-alice", role="reviewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", None):
            resp = asyncio.run(
                escalate_gate(
                    request_id,
                    EscalateRequest(escalate_to="admin-on-call"),
                    request,
                )
            )
        assert resp.ok is True
        assert resp.request_id == str(request_id)
        assert resp.escalated_to == "admin-on-call"

    def test_admin_can_escalate_any_claimed_gate(self) -> None:
        """Admin may escalate a gate claimed by any reviewer → success."""
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        gate_row = _make_gate_row(
            request_id,
            reviewer_id="reviewer-alice",
        )
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", None):
            resp = asyncio.run(
                escalate_gate(
                    request_id,
                    EscalateRequest(escalate_to="security-team"),
                    request,
                )
            )
        assert resp.ok is True
        assert resp.escalated_to == "security-team"

    def test_unclaimed_gate_can_be_escalated_by_any_authenticated_user(self) -> None:
        """Preserves prior v0.6.0 behavior: unclaimed gate is escalatable."""
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        gate_row = _make_gate_row(
            request_id,
            reviewer_id=None,
        )
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="viewer-bob", role="viewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", None):
            resp = asyncio.run(
                escalate_gate(
                    request_id,
                    EscalateRequest(escalate_to="admin-on-call"),
                    request,
                )
            )
        assert resp.ok is True


# ---------- Error preconditions (smoke) ---------------------------------


class TestEscalatePreconditions:
    """Edge cases that should short-circuit BEFORE the authz check."""

    def test_missing_gate_returns_404(self) -> None:
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        # SELECT returns None → gate not found.
        async def mock_execute(stmt: Any, params: Any = None) -> Any:
            result = MagicMock()
            result.mappings.return_value.first.return_value = None
            return result

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=mock_execute)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        engine = MagicMock()
        engine.begin.return_value = mock_ctx

        request = _make_request(user_id="viewer-bob", role="viewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    escalate_gate(
                        request_id,
                        EscalateRequest(escalate_to="admin"),
                        request,
                    )
                )
        assert exc_info.value.status_code == 404

    def test_resolved_gate_returns_409(self) -> None:
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )
        from datetime import datetime, timezone

        request_id = uuid4()
        gate_row = _make_gate_row(request_id, reviewer_id=None)
        gate_row["resolved_at"] = datetime.now(timezone.utc)
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    escalate_gate(
                        request_id,
                        EscalateRequest(escalate_to="admin"),
                        request,
                    )
                )
        assert exc_info.value.status_code == 409


# ---------- Audit row emission ------------------------------------------


class TestEscalateAuditRow:
    """``gates.escalated`` MUST be emitted with the documented metadata."""

    def test_audit_row_emitted_on_success(self) -> None:
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        gate_row = _make_gate_row(
            request_id,
            reviewer_id="reviewer-alice",
            agent_id="agent-beta",
            kind="HIGH_RISK",
        )
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")
        recorder = _RecordingAuditModule()

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", recorder):
            resp = asyncio.run(
                escalate_gate(
                    request_id,
                    EscalateRequest(escalate_to="security-team"),
                    request,
                )
            )
        assert resp.ok is True

        assert len(recorder.events) == 1
        evt = recorder.events[0]
        assert evt.kind == "gates.escalated"
        assert evt.agent_id == "agent-beta"
        md = evt.metadata
        assert md["request_id"] == str(request_id)
        assert md["gate_kind"] == "HIGH_RISK"
        assert md["reviewer_id_before"] == "reviewer-alice"
        assert md["reviewer_id_after"] is None
        assert md["by_user_id"] == "admin-root"
        assert md["role"] == "admin"
        assert md["escalated_to"] == "security-team"

    def test_audit_row_records_none_before_when_unclaimed(self) -> None:
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        gate_row = _make_gate_row(request_id, reviewer_id=None)
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="viewer-bob", role="viewer")
        recorder = _RecordingAuditModule()

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", recorder):
            asyncio.run(
                escalate_gate(
                    request_id,
                    EscalateRequest(escalate_to="admin"),
                    request,
                )
            )

        assert len(recorder.events) == 1
        md = recorder.events[0].metadata
        assert md["reviewer_id_before"] is None
        assert md["reviewer_id_after"] is None
        assert md["by_user_id"] == "viewer-bob"
        assert md["role"] == "viewer"

    def test_audit_row_not_emitted_on_403(self) -> None:
        """A 403 must NOT produce an audit row — otherwise a viewer can
        flood the audit log by probing claimed gates."""
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        gate_row = _make_gate_row(
            request_id,
            reviewer_id="reviewer-alice",
        )
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="viewer-bob", role="viewer")
        recorder = _RecordingAuditModule()

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", recorder):
            with pytest.raises(HTTPException):
                asyncio.run(
                    escalate_gate(
                        request_id,
                        EscalateRequest(escalate_to="admin"),
                        request,
                    )
                )
        assert recorder.events == []


# ---------- TOCTOU race (DA follow-up, v0.6.1) --------------------------


class TestEscalateRaceCondition:
    """v0.6.1 DA follow-up: SELECT-for-authz → UPDATE split is
    guarded by an ``IS NOT DISTINCT FROM :expected_reviewer`` predicate
    on the UPDATE and a RETURNING clause. If the UPDATE affects zero
    rows (another writer mutated reviewer_id in the race window), the
    endpoint MUST 409 — not silently succeed and emit a bogus
    ``gates.escalated`` audit row on stale state.
    """

    def test_lost_race_returns_409_and_no_audit_row(self) -> None:
        from codeatelier_governance.console.app import (
            EscalateRequest,
            escalate_gate,
        )

        request_id = uuid4()
        gate_row = _make_gate_row(
            request_id,
            reviewer_id="reviewer-alice",
        )

        async def mock_execute(stmt: Any, params: Any = None) -> Any:
            result = MagicMock()
            sql_text = str(stmt.text) if hasattr(stmt, "text") else str(stmt)
            if "SELECT" in sql_text and "governance_gates_pending" in sql_text:
                result.mappings.return_value.first.return_value = gate_row
            elif "UPDATE" in sql_text:
                # Simulate a racing writer: UPDATE affected zero rows
                # because reviewer_id was mutated between SELECT and UPDATE.
                result.first.return_value = None
            else:
                result.mappings.return_value.first.return_value = None
            return result

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=mock_execute)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        engine = MagicMock()
        engine.begin.return_value = mock_ctx

        request = _make_request(user_id="reviewer-alice", role="reviewer")
        recorder = _RecordingAuditModule()

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.audit_module", recorder):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    escalate_gate(
                        request_id,
                        EscalateRequest(escalate_to="admin-on-call"),
                        request,
                    )
                )
        assert exc_info.value.status_code == 409
        # No audit row on a lost race — otherwise the race gives a
        # viewer a write primitive into the audit log.
        assert recorder.events == []


# ---------- Silence ruff unused-import guards ---------------------------


def _silence_unused() -> None:
    _ = (patch,)
