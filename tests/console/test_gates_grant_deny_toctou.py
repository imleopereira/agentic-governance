"""v0.6.2 P0 — grant_gate / deny_gate TOCTOU race.

Pre-v0.6.2, ``POST /api/gates/{request_id}/grant`` and ``.../deny`` used
a SELECT-for-authz → UPDATE split. At default READ COMMITTED isolation
a racing ``claim`` could mutate ``reviewer_id`` between the two
statements: the authz check read ``reviewer_id = NULL`` and passed;
then the UPDATE committed anyway, emitting a bogus
``approval.granted`` / ``approval.denied`` audit row on a gate now
claimed by another reviewer. A viewer who could trigger a concurrent
claim thus had a silent claim-bypass primitive.

v0.6.2 closes the window by mirroring the ``escalate_gate`` fix from
v0.6.1: the UPDATE is pinned to ``reviewer_id IS NOT DISTINCT FROM
:expected_reviewer`` and uses ``RETURNING request_id`` to detect a
lost race. Zero rows returned → 409, NOT silent success. Admins
bypass the reviewer pin so incident-response flows still work when a
gate is claimed between SELECT and UPDATE; non-admins get the pin.

The 10 tests below cover:

* 5 × grant_gate: admin grants unclaimed, reviewer grants unclaimed,
  non-admin non-claimant blocked (403), TOCTOU race 409, audit-row
  suppression on 403/409.
* 5 × deny_gate: same matrix.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException


# ---------- Fixtures / helpers ------------------------------------------


def _make_mock_engine(
    gate_row: dict[str, Any],
    *,
    presence_operator_id: str | None = "someone-else",
    update_returns_row: bool = True,
) -> Any:
    """Build a mock engine for grant_gate / deny_gate tests.

    The SELECT in those handlers pulls
    ``request_id, agent_id, kind, token, action_hash, reviewer_id``
    from ``governance_gates_pending``. A second SELECT from
    ``governance_agent_presence`` drives the self-approval check.
    The UPDATE uses ``RETURNING request_id`` and checks ``.first()``
    for a row; ``update_returns_row=False`` simulates a lost race.
    """

    async def mock_execute(stmt: Any, params: Any = None) -> Any:
        result = MagicMock()
        sql_text = str(stmt.text) if hasattr(stmt, "text") else str(stmt)
        if "governance_gates_pending" in sql_text and "SELECT" in sql_text:
            result.mappings.return_value.first.return_value = gate_row
        elif "governance_agent_presence" in sql_text:
            result.mappings.return_value.first.return_value = {
                "operator_id": presence_operator_id,
            }
        elif "UPDATE" in sql_text:
            if update_returns_row:
                result.first.return_value = (str(params["rid"]),) if params else None
            else:
                # Lost race: another writer mutated reviewer_id between
                # SELECT and UPDATE; the IS NOT DISTINCT FROM predicate
                # rejects the UPDATE and RETURNING yields zero rows.
                result.first.return_value = None
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
        "token": None,
        "action_hash": None,
        "reviewer_id": reviewer_id,
    }


class _RecordingAuditModule:
    """Minimal stub that records ``log()`` calls."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def log(self, event: Any) -> None:
        self.events.append(event)


# ---------- grant_gate --------------------------------------------------


class TestGrantGateToctou:
    """v0.6.2 P0 — grant_gate TOCTOU fix."""

    def test_grant_unclaimed_works_for_admin(self) -> None:
        """Admin granting an unclaimed gate → success."""
        from codeatelier_governance.console.app import grant_gate

        request_id = uuid4()
        gate_row = _make_gate_row(request_id, reviewer_id=None)
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            result = asyncio.run(grant_gate(request_id, request))
        assert result["ok"] is True
        assert result["resolution"] == "granted"

    def test_grant_unclaimed_works_for_reviewer_who_has_no_claim(self) -> None:
        """Preserves v0.6.0 behavior: any authenticated user may grant an
        unclaimed gate (subject to self-approval check)."""
        from codeatelier_governance.console.app import grant_gate

        request_id = uuid4()
        gate_row = _make_gate_row(request_id, reviewer_id=None)
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="reviewer-carol", role="reviewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            result = asyncio.run(grant_gate(request_id, request))
        assert result["ok"] is True
        assert result["resolution"] == "granted"

    def test_grant_blocks_non_claimant_non_admin(self) -> None:
        """A reviewer (non-admin) cannot grant a gate claimed by someone
        else — 403 BEFORE the UPDATE, so no claim-bypass audit row."""
        from codeatelier_governance.console.app import grant_gate

        request_id = uuid4()
        gate_row = _make_gate_row(request_id, reviewer_id="reviewer-alice")
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="reviewer-carol", role="reviewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(grant_gate(request_id, request))
        assert exc_info.value.status_code == 403
        assert "claimed by another reviewer" in str(exc_info.value.detail)

    def test_grant_toctou_returns_409_on_race(self) -> None:
        """Simulate Admin A claiming between SELECT and UPDATE.

        At SELECT time the gate is unclaimed (reviewer_id=NULL), so the
        Python authz check passes for anyone. Admin A lands a claim
        mid-request — the ``IS NOT DISTINCT FROM :expected_reviewer``
        predicate (expected=NULL) now mismatches the DB state (NOT
        NULL), the UPDATE affects 0 rows, RETURNING yields nothing,
        and we 409 instead of silently succeeding.
        """
        from codeatelier_governance.console.app import grant_gate

        request_id = uuid4()
        # SELECT reads the pre-claim snapshot.
        gate_row = _make_gate_row(request_id, reviewer_id=None)
        # UPDATE affects 0 rows because a racing claim moved reviewer_id.
        engine = _make_mock_engine(gate_row, update_returns_row=False)
        request = _make_request(user_id="reviewer-bob", role="reviewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(grant_gate(request_id, request))
        assert exc_info.value.status_code == 409
        assert "claimed by another reviewer" in str(exc_info.value.detail)

    def test_grant_no_audit_row_on_409_or_403(self) -> None:
        """A 403 (non-claimant) and a 409 (lost race) must NOT emit
        ``approval.granted`` — otherwise a viewer can flood the audit
        log by probing claimed/racing gates."""
        from codeatelier_governance.console.app import grant_gate

        # --- 403 branch ---
        request_id_403 = uuid4()
        gate_row_403 = _make_gate_row(request_id_403, reviewer_id="reviewer-alice")
        engine_403 = _make_mock_engine(gate_row_403)
        request_403 = _make_request(user_id="reviewer-bob", role="reviewer")
        recorder_403 = _RecordingAuditModule()

        with patch("codeatelier_governance.console.app.engine", engine_403), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", recorder_403):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(grant_gate(request_id_403, request_403))
        assert exc_info.value.status_code == 403
        assert recorder_403.events == []

        # --- 409 branch ---
        request_id_409 = uuid4()
        gate_row_409 = _make_gate_row(request_id_409, reviewer_id=None)
        engine_409 = _make_mock_engine(gate_row_409, update_returns_row=False)
        request_409 = _make_request(user_id="reviewer-bob", role="reviewer")
        recorder_409 = _RecordingAuditModule()

        with patch("codeatelier_governance.console.app.engine", engine_409), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", recorder_409):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(grant_gate(request_id_409, request_409))
        assert exc_info.value.status_code == 409
        assert recorder_409.events == []


# ---------- deny_gate ---------------------------------------------------


class TestDenyGateToctou:
    """v0.6.2 P0 — deny_gate TOCTOU fix (mirrors grant_gate)."""

    def test_deny_unclaimed_works_for_admin(self) -> None:
        """Admin denying an unclaimed gate → success."""
        from codeatelier_governance.console.app import DenyRequest, deny_gate

        request_id = uuid4()
        gate_row = _make_gate_row(request_id, reviewer_id=None)
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            result = asyncio.run(
                deny_gate(
                    request_id,
                    DenyRequest(rationale="policy violation"),
                    request,
                )
            )
        assert result["ok"] is True
        assert result["resolution"] == "denied"

    def test_deny_unclaimed_works_for_reviewer_who_has_no_claim(self) -> None:
        """Preserves v0.6.0 behavior: any authenticated user may deny an
        unclaimed gate (subject to self-approval check)."""
        from codeatelier_governance.console.app import DenyRequest, deny_gate

        request_id = uuid4()
        gate_row = _make_gate_row(request_id, reviewer_id=None)
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="reviewer-carol", role="reviewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            result = asyncio.run(
                deny_gate(
                    request_id,
                    DenyRequest(rationale="out of scope"),
                    request,
                )
            )
        assert result["ok"] is True
        assert result["resolution"] == "denied"

    def test_deny_blocks_non_claimant_non_admin(self) -> None:
        """A reviewer (non-admin) cannot deny a gate claimed by someone
        else — 403 BEFORE the UPDATE."""
        from codeatelier_governance.console.app import DenyRequest, deny_gate

        request_id = uuid4()
        gate_row = _make_gate_row(request_id, reviewer_id="reviewer-alice")
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="reviewer-carol", role="reviewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    deny_gate(
                        request_id,
                        DenyRequest(rationale="out of scope"),
                        request,
                    )
                )
        assert exc_info.value.status_code == 403
        assert "claimed by another reviewer" in str(exc_info.value.detail)

    def test_deny_toctou_returns_409_on_race(self) -> None:
        """Simulate Admin A claiming between SELECT and UPDATE.

        Mirrors the grant case: SELECT sees reviewer_id=NULL, racing
        claim flips it to NOT NULL before the UPDATE; the pinned
        predicate mismatches, RETURNING yields zero rows, 409.
        """
        from codeatelier_governance.console.app import DenyRequest, deny_gate

        request_id = uuid4()
        gate_row = _make_gate_row(request_id, reviewer_id=None)
        engine = _make_mock_engine(gate_row, update_returns_row=False)
        request = _make_request(user_id="reviewer-bob", role="reviewer")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    deny_gate(
                        request_id,
                        DenyRequest(rationale="suspicious"),
                        request,
                    )
                )
        assert exc_info.value.status_code == 409
        assert "claimed by another reviewer" in str(exc_info.value.detail)

    def test_deny_no_audit_row_on_409_or_403(self) -> None:
        """A 403 (non-claimant) and a 409 (lost race) must NOT emit
        ``approval.denied`` — same threat model as the grant side."""
        from codeatelier_governance.console.app import DenyRequest, deny_gate

        # --- 403 branch ---
        request_id_403 = uuid4()
        gate_row_403 = _make_gate_row(request_id_403, reviewer_id="reviewer-alice")
        engine_403 = _make_mock_engine(gate_row_403)
        request_403 = _make_request(user_id="reviewer-bob", role="reviewer")
        recorder_403 = _RecordingAuditModule()

        with patch("codeatelier_governance.console.app.engine", engine_403), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", recorder_403):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    deny_gate(
                        request_id_403,
                        DenyRequest(rationale="x"),
                        request_403,
                    )
                )
        assert exc_info.value.status_code == 403
        assert recorder_403.events == []

        # --- 409 branch ---
        request_id_409 = uuid4()
        gate_row_409 = _make_gate_row(request_id_409, reviewer_id=None)
        engine_409 = _make_mock_engine(gate_row_409, update_returns_row=False)
        request_409 = _make_request(user_id="reviewer-bob", role="reviewer")
        recorder_409 = _RecordingAuditModule()

        with patch("codeatelier_governance.console.app.engine", engine_409), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", recorder_409):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    deny_gate(
                        request_id_409,
                        DenyRequest(rationale="x"),
                        request_409,
                    )
                )
        assert exc_info.value.status_code == 409
        assert recorder_409.events == []


# ---------- Silence ruff unused-import guards ---------------------------


def _silence_unused() -> None:
    _ = (patch,)
