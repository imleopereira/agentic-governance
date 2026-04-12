"""Tests for console FastAPI endpoints.

Gap #6: Console endpoint tests (health, auth, and protected endpoints).
Gap #7: SSE endpoint test (/api/stream/events).

These tests use FastAPI's TestClient. Most endpoints require auth and a live
Postgres, so we test the health endpoint (unauthenticated), the SSE endpoint
(auth check), and dev-mode middleware behavior.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest


def _get_test_client() -> Any:
    """Create a fresh TestClient with dev mode enabled.

    Re-imports the app module so env var changes take effect.
    """
    from starlette.testclient import TestClient

    # The app is already created at module level, but we can set env vars
    # before requesting. DEV_MODE skips auth for non-login endpoints.
    return TestClient


class TestHealthEndpoint:
    """Gap #6: /api/health is unauthenticated and always returns ok."""

    def test_health_returns_ok(self) -> None:
        from starlette.testclient import TestClient
        from codeatelier_governance.console.app import app, health

        # Health endpoint bypasses auth (checked in authenticate dependency)
        # We call the endpoint function directly to avoid lifespan issues
        import asyncio

        result = asyncio.run(health())
        assert result["ok"] is True
        assert "version" in result

    def test_health_version_format(self) -> None:
        import asyncio

        from codeatelier_governance.console.app import health

        result = asyncio.run(health())
        # Version should be a semver-like string
        parts = result["version"].split(".")
        assert len(parts) >= 2


class TestAuthenticateDependency:
    """Gap #6: Auth middleware behavior for protected endpoints."""

    def test_dev_mode_sets_admin_role(self) -> None:
        """In dev mode, authenticate should set role=admin without checking cookies."""
        import asyncio
        from unittest.mock import MagicMock

        from codeatelier_governance.console.app import authenticate

        # Patch DEV_MODE at module level
        with patch("codeatelier_governance.console.app.DEV_MODE", True):
            request = MagicMock()
            request.url.path = "/api/events"
            asyncio.run(authenticate(request))
            assert request.state.user_id == "dev"
            assert request.state.role == "admin"

    def test_health_endpoint_skips_auth(self) -> None:
        """The /api/health path should skip all auth checks."""
        import asyncio
        from unittest.mock import MagicMock

        from codeatelier_governance.console.app import authenticate

        with patch("codeatelier_governance.console.app.DEV_MODE", False):
            request = MagicMock()
            request.url.path = "/api/health"
            # Should NOT raise even without cookies or tokens
            asyncio.run(authenticate(request))

    def test_unauthenticated_request_raises_401(self) -> None:
        """Without dev mode, cookies, or token, auth should raise 401."""
        import asyncio
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        from codeatelier_governance.console.app import authenticate

        with patch("codeatelier_governance.console.app.DEV_MODE", False), \
             patch("codeatelier_governance.console.app.CONSOLE_TOKEN", ""), \
             patch("codeatelier_governance.console.app.engine", None):
            request = MagicMock()
            request.url.path = "/api/events"
            request.cookies.get.return_value = None
            request.headers.get.return_value = ""
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(authenticate(request))
            assert exc_info.value.status_code == 401

    def test_legacy_token_auth(self) -> None:
        """A valid Bearer token should authenticate as legacy-token/viewer."""
        import asyncio
        from unittest.mock import MagicMock

        from codeatelier_governance.console.app import authenticate

        with patch("codeatelier_governance.console.app.DEV_MODE", False), \
             patch("codeatelier_governance.console.app.CONSOLE_TOKEN", "test-secret-token"):
            request = MagicMock()
            request.url.path = "/api/events"
            request.cookies.get.return_value = None
            request.headers.get.return_value = "Bearer test-secret-token"
            asyncio.run(authenticate(request))
            assert request.state.user_id == "legacy-token"
            assert request.state.role == "viewer"


class TestRequireRole:
    """Gap #6: Role-based access control dependency."""

    def test_require_role_admin_passes(self) -> None:
        """Admin role should pass any role check."""
        import asyncio
        from unittest.mock import MagicMock

        from codeatelier_governance.console.app import require_role

        dep = require_role("operator")
        # Extract the actual check function from the Depends wrapper
        # require_role returns Depends(check), so we need the inner function
        check_fn = dep.dependency

        request = MagicMock()
        request.state.role = "admin"
        asyncio.run(check_fn(request))  # Should not raise

    def test_require_role_wrong_role_raises_403(self) -> None:
        """Wrong role should raise 403."""
        import asyncio
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        from codeatelier_governance.console.app import require_role

        dep = require_role("operator")
        check_fn = dep.dependency

        request = MagicMock()
        request.state.role = "viewer"
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(check_fn(request))
        assert exc_info.value.status_code == 403


class TestAccountEnumerationParity:
    """Gap #9: Disabled account, wrong password, and nonexistent user must all
    return the identical HTTP 401 with the same message to prevent account
    enumeration attacks."""

    def test_disabled_account_same_error_as_wrong_password(self) -> None:
        """All three failure paths (nonexistent user, wrong password, disabled
        account) must return HTTP 401 with identical body."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from fastapi import HTTPException

        from codeatelier_governance.console.app import login, LoginRequest

        def _make_engine_returning(row: dict | None) -> MagicMock:
            """Build a mock engine that returns *row* from the user SELECT.

            The login endpoint calls engine.begin() (session cleanup DELETE)
            then engine.connect() (user lookup SELECT). Both are async
            context managers. The SELECT result needs .mappings().first()
            to return the row.
            """
            mock_engine = MagicMock()

            # Result for the SELECT query
            mock_select_result = MagicMock()
            mock_select_result.mappings.return_value.first.return_value = row

            # Connection for engine.connect() — returns the SELECT result
            mock_connect_conn = AsyncMock()
            mock_connect_conn.execute = AsyncMock(return_value=mock_select_result)
            mock_connect_ctx = AsyncMock()
            mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_connect_conn)
            mock_connect_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_engine.connect.return_value = mock_connect_ctx

            # Connection for engine.begin() — the DELETE is fire-and-forget
            mock_begin_conn = AsyncMock()
            mock_begin_ctx = AsyncMock()
            mock_begin_ctx.__aenter__ = AsyncMock(return_value=mock_begin_conn)
            mock_begin_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_engine.begin.return_value = mock_begin_ctx

            return mock_engine

        expected_detail = "Invalid username or password."

        # --- Case 1: nonexistent user (row is None) ---
        with patch("codeatelier_governance.console.app.engine", _make_engine_returning(None)), \
             patch("codeatelier_governance.console.app._check_rate_limit", return_value=None), \
             patch("codeatelier_governance.console.app._record_login_attempt"):
            request = MagicMock()
            request.client.host = "127.0.0.1"
            body = LoginRequest(username="ghost", password="anything")
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(login(body, request))
            assert exc_info.value.status_code == 401
            nonexistent_msg = exc_info.value.detail

        # --- Case 2: wrong password (row exists, verify_password returns False) ---
        fake_row_wrong_pw = {
            "user_id": "uid-1",
            "password_hash": "$invalid$hash",
            "role": "operator",
            "disabled": False,
        }
        with patch("codeatelier_governance.console.app.engine", _make_engine_returning(fake_row_wrong_pw)), \
             patch("codeatelier_governance.console.app._check_rate_limit", return_value=None), \
             patch("codeatelier_governance.console.app._record_login_attempt"), \
             patch("codeatelier_governance.console.app.verify_password", return_value=False):
            request = MagicMock()
            request.client.host = "127.0.0.1"
            body = LoginRequest(username="alice", password="wrong")
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(login(body, request))
            assert exc_info.value.status_code == 401
            wrong_pw_msg = exc_info.value.detail

        # --- Case 3: disabled account (row exists, password matches, disabled=True) ---
        fake_row_disabled = {
            "user_id": "uid-2",
            "password_hash": "$valid$hash",
            "role": "operator",
            "disabled": True,
        }
        with patch("codeatelier_governance.console.app.engine", _make_engine_returning(fake_row_disabled)), \
             patch("codeatelier_governance.console.app._check_rate_limit", return_value=None), \
             patch("codeatelier_governance.console.app._record_login_attempt"), \
             patch("codeatelier_governance.console.app.verify_password", return_value=True):
            request = MagicMock()
            request.client.host = "127.0.0.1"
            body = LoginRequest(username="bob", password="correct")
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(login(body, request))
            assert exc_info.value.status_code == 401
            disabled_msg = exc_info.value.detail

        # All three MUST be identical
        assert nonexistent_msg == expected_detail
        assert wrong_pw_msg == expected_detail
        assert disabled_msg == expected_detail


class TestSelfApprovalPrevention:
    """Gap #8: Console grant/deny endpoints must reject self-approval — an
    operator should not be able to approve/deny a gate request that their
    own agent created."""

    @staticmethod
    def _make_mock_engine(
        gate_row: dict[str, Any],
        presence_operator_id: str | None,
        presence_row_exists: bool = True,
    ) -> Any:
        """Build a mock engine that returns *gate_row* from the gates SELECT
        and *presence_operator_id* from the presence SELECT."""
        from unittest.mock import AsyncMock, MagicMock

        call_count = 0

        async def mock_execute(stmt: Any, params: Any = None) -> Any:
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            sql_text = str(stmt.text) if hasattr(stmt, "text") else str(stmt)
            if "governance_gates_pending" in sql_text and "SELECT" in sql_text:
                result.mappings.return_value.first.return_value = gate_row
            elif "governance_agent_presence" in sql_text:
                if presence_row_exists:
                    result.mappings.return_value.first.return_value = {
                        "operator_id": presence_operator_id,
                    }
                else:
                    result.mappings.return_value.first.return_value = None
            else:
                # UPDATE statements
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

    @staticmethod
    def _make_request(user_id: str) -> Any:
        """Build a mock Request with request.state.user_id set."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.state.user_id = user_id
        return request

    def test_grant_rejects_self_approval(self) -> None:
        """Granting your own agent's gate request should return 403."""
        import asyncio
        from uuid import uuid4

        from fastapi import HTTPException

        from codeatelier_governance.console.app import grant_gate

        request_id = uuid4()
        agent_id = "my-agent"
        user_id = "operator-1"

        fake_row = {
            "request_id": str(request_id),
            "agent_id": agent_id,
            "kind": "high_risk",
            "token": None,
            "action_hash": None,
        }
        mock_engine = self._make_mock_engine(fake_row, presence_operator_id=user_id)
        request = self._make_request(user_id)

        with patch("codeatelier_governance.console.app.engine", mock_engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(grant_gate(request_id, request))
            assert exc_info.value.status_code == 403
            assert "Cannot approve" in str(exc_info.value.detail)

    def test_grant_allows_different_operator(self) -> None:
        """Grant should succeed when operator_id differs from user_id."""
        import asyncio
        from uuid import uuid4

        from codeatelier_governance.console.app import grant_gate

        request_id = uuid4()
        agent_id = "agent-of-alice"
        user_id = "bob"

        fake_row = {
            "request_id": str(request_id),
            "agent_id": agent_id,
            "kind": "high_risk",
            "token": None,
            "action_hash": None,
        }
        mock_engine = self._make_mock_engine(fake_row, presence_operator_id="alice")
        request = self._make_request(user_id)

        with patch("codeatelier_governance.console.app.engine", mock_engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            result = asyncio.run(grant_gate(request_id, request))
            assert result["ok"] is True
            assert result["resolution"] == "granted"

    def test_grant_blocks_when_no_operator_id(self) -> None:
        """Grant should fail-closed when agent has no operator_id."""
        import asyncio
        from uuid import uuid4

        from fastapi import HTTPException

        from codeatelier_governance.console.app import grant_gate

        request_id = uuid4()
        agent_id = "agent-no-owner"
        user_id = "operator-1"

        fake_row = {
            "request_id": str(request_id),
            "agent_id": agent_id,
            "kind": "high_risk",
            "token": None,
            "action_hash": None,
        }
        mock_engine = self._make_mock_engine(fake_row, presence_operator_id=None)
        request = self._make_request(user_id)

        with patch("codeatelier_governance.console.app.engine", mock_engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(grant_gate(request_id, request))
            assert exc_info.value.status_code == 403
            assert "operator_id" in str(exc_info.value.detail)

    def test_deny_rejects_self_approval(self) -> None:
        """Denying your own agent's gate request should return 403."""
        import asyncio
        from uuid import uuid4

        from fastapi import HTTPException

        from codeatelier_governance.console.app import deny_gate

        request_id = uuid4()
        agent_id = "my-agent"
        user_id = "operator-1"

        fake_row = {
            "request_id": str(request_id),
            "agent_id": agent_id,
            "kind": "high_risk",
            "token": None,
            "action_hash": None,
        }
        mock_engine = self._make_mock_engine(fake_row, presence_operator_id=user_id)
        request = self._make_request(user_id)

        with patch("codeatelier_governance.console.app.engine", mock_engine), \
             patch("codeatelier_governance.console.app.AUDIT_SECRET", "x" * 32), \
             patch("codeatelier_governance.console.app.audit_module", None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(deny_gate(request_id, request))
            assert exc_info.value.status_code == 403
            assert "Cannot approve" in str(exc_info.value.detail)


class TestSSEEndpointAuth:
    """Gap #7: /api/stream/events requires authentication."""

    def test_sse_rejects_unauthenticated(self) -> None:
        """SSE endpoint should reject requests without session cookie or token."""
        import asyncio
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        from codeatelier_governance.console.app import stream_events

        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(stream_events(request, last_event_id=None))
        assert exc_info.value.status_code == 401

    def test_sse_accepts_token_header(self) -> None:
        """SSE endpoint should accept x-governance-token header."""
        import asyncio
        from unittest.mock import MagicMock

        from codeatelier_governance.console.app import stream_events

        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = "some-token"

        with patch("codeatelier_governance.console.app.engine", None):
            # With no engine, it should still return a StreamingResponse
            # (the generator will just sleep)
            result = asyncio.run(stream_events(request, last_event_id=None))
            assert result.status_code == 200
            assert result.media_type == "text/event-stream"
