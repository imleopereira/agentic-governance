"""Regression: grant_gate / deny_gate token HMAC verification.

Pre-fix bug (shipped since v0.2.0): the console's ``grant_gate`` and
``deny_gate`` handlers computed ``expected = hmac(secret, request_id)``
and compared it against the full stored token. The stored token is
actually a 4-field signed string produced by
``gates.tokens.make_token``:

    f"{request_id}:{action_hash}:{expires_at_iso}:{hmac_hex}"

The two shapes can never match — so any gate written by the real SDK
path (``sdk.gates.request``) returned "Token HMAC verification failed."
when an operator tried to approve it. The only reason pre-fix tests
passed was that they all used ``token: None`` in the mock gate row,
which skipped the verification branch entirely.

This file exercises the REAL signed-token path end to end. It mints a
token via the public SDK surface, feeds it to the console handler,
and confirms the handler accepts the legitimate token + rejects
tampered ones. The test is synchronous-capable (no DB, no network).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from codeatelier_governance.gates.tokens import make_token


# ---------- helpers -----------------------------------------------------


def _make_mock_engine(gate_row: dict[str, Any]) -> Any:
    """Mock engine matching the shape expected by grant_gate/deny_gate."""

    async def mock_execute(stmt: Any, params: Any = None) -> Any:
        result = MagicMock()
        sql_text = str(stmt.text) if hasattr(stmt, "text") else str(stmt)
        if "governance_gates_pending" in sql_text and "SELECT" in sql_text:
            result.mappings.return_value.first.return_value = gate_row
        elif "governance_agent_presence" in sql_text:
            result.mappings.return_value.first.return_value = {
                "operator_id": "some-other-operator",
            }
        elif "UPDATE" in sql_text:
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
    request = MagicMock()
    request.state.user_id = user_id
    request.state.role = role
    return request


# ---------- grant_gate token verification -------------------------------


class TestGrantGateTokenHMAC:
    """Regression: grant_gate accepts a valid signed token and rejects bad ones."""

    def _fresh_secret(self) -> bytes:
        # 32 bytes — satisfies gates secret strength check.
        return b"a" * 32

    def _fresh_token_row(
        self,
        request_id: UUID,
        secret: bytes,
    ) -> dict[str, Any]:
        action_hash = "deadbeef" * 8  # 64-char sha256-shaped placeholder
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        token = make_token(
            secret=secret,
            request_id=request_id,
            action_hash=action_hash,
            expires_at=expires_at,
        )
        return {
            "request_id": str(request_id),
            "agent_id": "invoice-approver",
            "kind": "send_email",
            "token": token,
            "action_hash": action_hash,
            "reviewer_id": None,
        }

    def test_valid_token_from_sdk_is_accepted(self) -> None:
        """The real token shape emitted by ``make_token`` must verify
        under the console's grant handler. This is the bug that shipped
        in v0.2.0 and was masked until v0.6.2 because every test used
        ``token: None``."""
        from codeatelier_governance.console.app import grant_gate

        secret = self._fresh_secret()
        request_id = uuid4()
        gate_row = self._fresh_token_row(request_id, secret)
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch(
                 "codeatelier_governance.console.app.AUDIT_SECRET",
                 secret.decode("utf-8"),
             ), \
             patch("codeatelier_governance.console.app.audit_module", None):
            result = asyncio.run(grant_gate(request_id, request))
        assert result["ok"] is True
        assert result["resolution"] == "granted"

    def test_tampered_token_rejected(self) -> None:
        """A token with a mutated hmac suffix must NOT verify."""
        from codeatelier_governance.console.app import grant_gate

        secret = self._fresh_secret()
        request_id = uuid4()
        gate_row = self._fresh_token_row(request_id, secret)
        # Flip the last hex nibble of the mac to a different char.
        last = gate_row["token"][-1]
        flipped = "0" if last != "0" else "1"
        gate_row["token"] = gate_row["token"][:-1] + flipped
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch(
                 "codeatelier_governance.console.app.AUDIT_SECRET",
                 secret.decode("utf-8"),
             ), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(grant_gate(request_id, request))
        assert exc_info.value.status_code == 400
        assert "Token HMAC verification failed" in str(exc_info.value.detail)

    def test_token_signed_with_wrong_secret_rejected(self) -> None:
        """Changing the audit secret between seed-time and grant-time
        must invalidate the token — the stored HMAC no longer matches
        what the handler computes under the new secret."""
        from codeatelier_governance.console.app import grant_gate

        minted_secret = self._fresh_secret()
        verify_secret = b"b" * 32  # different secret
        request_id = uuid4()
        gate_row = self._fresh_token_row(request_id, minted_secret)
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch(
                 "codeatelier_governance.console.app.AUDIT_SECRET",
                 verify_secret.decode("utf-8"),
             ), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(grant_gate(request_id, request))
        assert exc_info.value.status_code == 400
        assert "Token HMAC verification failed" in str(exc_info.value.detail)

    def test_action_hash_tampering_rejected(self) -> None:
        """If the stored ``action_hash`` column is mutated (hypothetical
        tamper — append-only trigger should prevent it) the parsed
        action_hash from the token must mismatch and the handler
        rejects the request."""
        from codeatelier_governance.console.app import grant_gate

        secret = self._fresh_secret()
        request_id = uuid4()
        gate_row = self._fresh_token_row(request_id, secret)
        # Token was signed against the original action_hash; rewrite
        # the column only (simulating a row-level tamper).
        gate_row["action_hash"] = "cafef00d" * 8
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch(
                 "codeatelier_governance.console.app.AUDIT_SECRET",
                 secret.decode("utf-8"),
             ), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(grant_gate(request_id, request))
        assert exc_info.value.status_code == 400
        assert "Token HMAC verification failed" in str(exc_info.value.detail)


# ---------- deny_gate token verification --------------------------------


class TestDenyGateTokenHMAC:
    """Same regression as grant — deny_gate must verify real signed tokens."""

    def test_valid_token_from_sdk_is_accepted_for_deny(self) -> None:
        from codeatelier_governance.console.app import DenyRequest, deny_gate

        secret = b"a" * 32
        request_id = uuid4()
        action_hash = "deadbeef" * 8
        token = make_token(
            secret=secret,
            request_id=request_id,
            action_hash=action_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        gate_row = {
            "request_id": str(request_id),
            "agent_id": "invoice-approver",
            "kind": "send_email",
            "token": token,
            "action_hash": action_hash,
            "reviewer_id": None,
        }
        engine = _make_mock_engine(gate_row)
        request = _make_request(user_id="admin-root", role="admin")

        with patch("codeatelier_governance.console.app.engine", engine), \
             patch(
                 "codeatelier_governance.console.app.AUDIT_SECRET",
                 secret.decode("utf-8"),
             ), \
             patch("codeatelier_governance.console.app.audit_module", None):
            result = asyncio.run(
                deny_gate(
                    request_id,
                    DenyRequest(rationale="not approved"),
                    request,
                )
            )
        assert result["ok"] is True
        assert result["resolution"] == "denied"
