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
