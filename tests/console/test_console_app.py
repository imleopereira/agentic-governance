"""Tests for console/app.py — redaction, CORS validation, posture bounds,
posture-endpoint status literal contract, rationale XSS safety,
batch-approve cap, and self-approval enforcement."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from codeatelier_governance.console.app import (
    _BATCH_APPROVE_MAX,
    BatchApproveRequest,
    DenyRequest,
    _redact_metadata,
    _validate_cors_origins,
)


# -- SDK-2: Recursive metadata redaction --------------------------------------


class TestRedactMetadata:
    def test_flat_redaction(self) -> None:
        meta = {"user": "alice", "api_key": "sk-secret123"}
        result = _redact_metadata(meta)
        assert result == {"user": "alice", "api_key": "***REDACTED***"}

    def test_nested_dict_redaction(self) -> None:
        meta = {"config": {"api_key": "sk-secret", "region": "us-east"}}
        result = _redact_metadata(meta)
        assert result == {"config": {"api_key": "***REDACTED***", "region": "us-east"}}

    def test_deeply_nested_redaction(self) -> None:
        meta = {"a": {"b": {"c": {"password": "hunter2", "value": 42}}}}
        result = _redact_metadata(meta)
        assert result["a"]["b"]["c"]["password"] == "***REDACTED***"
        assert result["a"]["b"]["c"]["value"] == 42

    def test_list_of_dicts_redaction(self) -> None:
        meta = {"items": [{"token": "abc"}, {"name": "safe"}]}
        result = _redact_metadata(meta)
        assert result["items"][0]["token"] == "***REDACTED***"
        assert result["items"][1]["name"] == "safe"

    def test_mixed_nesting(self) -> None:
        meta = {
            "outer": "safe",
            "nested": {
                "list": [{"secret": "x"}, "plain"],
                "authorization": "bearer xyz",
            },
        }
        result = _redact_metadata(meta)
        assert result["outer"] == "safe"
        assert result["nested"]["authorization"] == "***REDACTED***"
        assert result["nested"]["list"][0]["secret"] == "***REDACTED***"
        assert result["nested"]["list"][1] == "plain"

    def test_empty_dict(self) -> None:
        assert _redact_metadata({}) == {}

    def test_no_sensitive_keys(self) -> None:
        meta = {"status": "ok", "count": 5}
        assert _redact_metadata(meta) == meta


# -- SDK-5: CORS wildcard rejection -------------------------------------------


class TestCorsValidation:
    def test_wildcard_rejected(self) -> None:
        with pytest.raises(ValueError, match="CORS wildcard"):
            _validate_cors_origins(["*"])

    def test_wildcard_in_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="CORS wildcard"):
            _validate_cors_origins(["http://localhost:3000", "*"])

    def test_specific_origins_pass(self) -> None:
        _validate_cors_origins(["http://localhost:3000", "https://console.example.com"])

    def test_empty_list_passes(self) -> None:
        _validate_cors_origins([])


# -- Contract: /api/posture status literals ----------------------------------
#
# The v4 frontend's `console/src/lib/v4/statusMap.ts` helper treats the
# posture endpoint's per-module `status` field as a closed literal union
# {"PASS", "WARN", "FAIL"}. Any drift on the backend side — a typo like
# "pass", a new literal like "HEALTHY", a rename to "OK" — will make the
# frontend's mapAgentStatus fall through to the "degraded" unknown-status
# branch for every agent. This test locks the contract textually so
# either side of the boundary will break loudly on drift.


APP_PY = (
    Path(__file__).parent.parent.parent
    / "src"
    / "codeatelier_governance"
    / "console"
    / "app.py"
)
POSTURE_STATUS_LITERALS = frozenset({"PASS", "WARN", "FAIL"})


class TestPostureStatusContract:
    def _posture_handler_body(self) -> str:
        """Return the textual body of the `/api/posture` handler."""
        source = APP_PY.read_text(encoding="utf-8")
        # Match from the @app.get("/api/posture" decorator to the next
        # top-level @app.<verb>( decorator (or end of file).
        match = re.search(
            r'@app\.get\(\s*"/api/posture".*?(?=\n@app\.\w+\s*\()',
            source,
            re.DOTALL,
        )
        assert match, "Could not locate /api/posture handler in app.py"
        return match.group(0)

    def test_posture_handler_is_locatable(self) -> None:
        body = self._posture_handler_body()
        assert len(body) > 100, "posture handler body suspiciously short"

    def test_posture_emits_only_allowed_status_literals(self) -> None:
        body = self._posture_handler_body()
        found: set[str] = set()
        for m in re.finditer(r'"status"\s*:\s*"([^"]+)"', body):
            found.add(m.group(1))
        for m in re.finditer(r'\bcost_status\s*=\s*"([^"]+)"', body):
            found.add(m.group(1))

        assert found, (
            "No status literals discovered in /api/posture handler. "
            "If the handler moved, update the locator regex above."
        )
        unexpected = found - POSTURE_STATUS_LITERALS
        assert not unexpected, (
            f"Posture endpoint emits unexpected status literals {sorted(unexpected)}. "
            "The v4 frontend mapper (console/src/lib/v4/statusMap.ts) only "
            f"accepts {sorted(POSTURE_STATUS_LITERALS)} and will fall through "
            "to 'degraded' for anything else. Update both sides together."
        )

    def test_posture_uses_all_three_literals_somewhere(self) -> None:
        """Every literal in the contract set must appear at least once.

        If the backend stops emitting a literal (e.g. `audit.status`
        becomes a `str | None` instead of always `"PASS"`), the frontend
        branch for that value becomes dead code and should be removed in
        the same PR — this test forces the conversation.
        """
        body = self._posture_handler_body()
        found: set[str] = set()
        for m in re.finditer(r'"([A-Z]+)"', body):
            v = m.group(1)
            if v in POSTURE_STATUS_LITERALS:
                found.add(v)
        missing = POSTURE_STATUS_LITERALS - found
        assert not missing, (
            f"/api/posture handler no longer emits literals {sorted(missing)}. "
            "If this is intentional, remove the matching branch from "
            "console/src/lib/v4/statusMap.ts in the same commit."
        )


# -- Security: rationale XSS safety -----------------------------------------
#
# The deny endpoint accepts a `rationale` field and stores it in:
#   1. governance_gates_pending.rationale   (plain VARCHAR, not rendered as HTML)
#   2. audit event metadata                 (JSON, served as application/json)
# Because both storage paths are JSON/SQL — never an HTML template — the
# browser never interprets the value as markup. This test locks that contract:
#   a) DenyRequest accepts the XSS payload without modification (no stripping)
#   b) app.py never uses an HTML-rendering response class for the deny route


class TestRationaleXSS:
    _XSS = '<script>alert(1)</script>'

    def test_deny_request_accepts_xss_payload_verbatim(self) -> None:
        """Pydantic model must not silently strip or escape the payload."""
        req = DenyRequest(rationale=self._XSS)
        assert req.rationale == self._XSS

    def test_deny_route_never_uses_html_response(self) -> None:
        """The /deny handler must return JSON, never an HTML template."""
        source = APP_PY.read_text(encoding="utf-8")
        # Locate the deny_gate function body (from its decorator to the next
        # top-level decorator or end-of-file).
        match = re.search(
            r'@app\.post\(\s*"/api/gates/\{request_id\}/deny".*?(?=\n@app\.\w+\s*\(|\Z)',
            source,
            re.DOTALL,
        )
        assert match, "Could not locate /deny handler in app.py"
        body = match.group(0)
        # No HTML response primitives inside the handler
        assert "HTMLResponse" not in body
        assert "TemplateResponse" not in body
        assert "render_template" not in body

    def test_deny_rationale_stored_in_audit_metadata(self) -> None:
        """Audit metadata dict must include 'rationale' key in the deny handler."""
        source = APP_PY.read_text(encoding="utf-8")
        match = re.search(
            r'@app\.post\(\s*"/api/gates/\{request_id\}/deny".*?(?=\n@app\.\w+\s*\(|\Z)',
            source,
            re.DOTALL,
        )
        assert match, "Could not locate /deny handler in app.py"
        body = match.group(0)
        assert '"rationale"' in body, (
            "Deny handler must include 'rationale' in audit metadata so "
            "it enters the HMAC chain and becomes tamper-evident."
        )


# -- Contract: batch-approve hard cap ----------------------------------------


class TestBatchApproveContract:
    def test_cap_constant_is_fifty(self) -> None:
        assert _BATCH_APPROVE_MAX == 50

    def test_batch_request_rejects_over_cap(self) -> None:
        """Pydantic must reject lists exceeding the hard cap."""
        ids = [uuid.uuid4() for _ in range(51)]
        with pytest.raises(ValidationError):
            BatchApproveRequest(request_ids=ids)

    def test_batch_request_accepts_at_cap(self) -> None:
        ids = [uuid.uuid4() for _ in range(50)]
        req = BatchApproveRequest(request_ids=ids)
        assert len(req.request_ids) == 50

    def test_batch_request_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            BatchApproveRequest(request_ids=[])

    def test_batch_handler_enforces_cap_at_runtime(self) -> None:
        """The handler has a belt-and-suspenders len() guard after Pydantic."""
        source = APP_PY.read_text(encoding="utf-8")
        match = re.search(
            r'@app\.post\(\s*"/api/gates/batch-approve".*?(?=\n@app\.\w+\s*\(|\Z)',
            source,
            re.DOTALL,
        )
        assert match, "Could not locate /batch-approve handler in app.py"
        body = match.group(0)
        assert "_BATCH_APPROVE_MAX" in body, (
            "batch_approve handler must contain a runtime len() guard against "
            "_BATCH_APPROVE_MAX in addition to Pydantic's field constraint."
        )


# -- Contract: self-approval prevention in batch-approve ---------------------


class TestBatchApproveSelfApproval:
    def test_self_approval_check_called_in_batch_loop(self) -> None:
        """_check_self_approval must be called for every item in the batch loop."""
        source = APP_PY.read_text(encoding="utf-8")
        match = re.search(
            r'@app\.post\(\s*"/api/gates/batch-approve".*?(?=\n@app\.\w+\s*\(|\Z)',
            source,
            re.DOTALL,
        )
        assert match, "Could not locate /batch-approve handler in app.py"
        body = match.group(0)
        assert "_check_self_approval" in body, (
            "batch_approve must call _check_self_approval for each gate. "
            "Without this, an operator can self-approve actions in bulk."
        )

    def test_self_approval_failure_lands_in_failed_list(self) -> None:
        """A self-approval hit must add an entry to the failed list, not raise."""
        source = APP_PY.read_text(encoding="utf-8")
        match = re.search(
            r'@app\.post\(\s*"/api/gates/batch-approve".*?(?=\n@app\.\w+\s*\(|\Z)',
            source,
            re.DOTALL,
        )
        assert match, "Could not locate /batch-approve handler in app.py"
        body = match.group(0)
        # The handler must catch HTTPException from _check_self_approval and
        # record it as a failed item rather than aborting the whole batch.
        assert "self_approval_blocked" in body, (
            "batch_approve must record 'self_approval_blocked' in the failed "
            "list when self-approval is detected, not abort the whole request."
        )
