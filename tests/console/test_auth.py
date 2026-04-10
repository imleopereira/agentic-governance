"""Tests for console auth model (AUTH-1)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from codeatelier_governance.console.auth import (
    Role,
    create_session_id,
    hash_password,
    session_expires_at,
    verify_password,
)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
class TestPasswordHashing:
    def test_hash_then_verify(self) -> None:
        """A hashed password should verify correctly."""
        pw = "s3cure-passw0rd!"
        hashed = hash_password(pw)
        assert verify_password(pw, hashed) is True

    def test_wrong_password_fails(self) -> None:
        hashed = hash_password("correct-password")
        assert verify_password("wrong-password", hashed) is False

    def test_hash_is_not_plaintext(self) -> None:
        pw = "my-secret"
        hashed = hash_password(pw)
        assert pw not in hashed

    def test_hash_uses_pbkdf2_format(self) -> None:
        hashed = hash_password("test")
        parts = hashed.split(":")
        assert len(parts) == 4
        assert parts[0] == "pbkdf2"
        assert int(parts[1]) >= 600_000

    def test_different_hashes_for_same_password(self) -> None:
        """Salt ensures different hashes each time."""
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2
        assert verify_password("same", h1) is True
        assert verify_password("same", h2) is True

    def test_verify_rejects_garbage_hash(self) -> None:
        assert verify_password("test", "not-a-valid-hash") is False
        assert verify_password("test", "") is False

    def test_verify_rejects_wrong_format(self) -> None:
        assert verify_password("test", "md5:abc:def") is False


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
class TestSessionHelpers:
    def test_create_session_id_is_uuid(self) -> None:
        sid = create_session_id()
        assert len(str(sid)) == 36

    def test_session_expires_at_default_8h(self) -> None:
        before = datetime.now(timezone.utc)
        expires = session_expires_at()
        after = datetime.now(timezone.utc)
        assert expires > before + timedelta(hours=7, minutes=59)
        assert expires < after + timedelta(hours=8, minutes=1)

    def test_session_expires_at_custom_ttl(self) -> None:
        expires = session_expires_at(ttl_hours=1)
        now = datetime.now(timezone.utc)
        diff = (expires - now).total_seconds()
        assert 3500 < diff < 3700


# ---------------------------------------------------------------------------
# Role enum
# ---------------------------------------------------------------------------
class TestRole:
    def test_viewer_role(self) -> None:
        assert Role.VIEWER == "viewer"

    def test_admin_role(self) -> None:
        assert Role.ADMIN == "admin"


# ---------------------------------------------------------------------------
# Console app auth (unit tests using HTTPX test client)
# ---------------------------------------------------------------------------
class TestConsoleAuth:
    """Tests for the console app auth flow using FastAPI TestClient."""

    @pytest.fixture
    def _mock_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOVERNANCE_CONSOLE_DEV_MODE", "true")
        monkeypatch.setenv("GOVERNANCE_DATABASE_URL", "postgresql://x:x@localhost/x")
        monkeypatch.setenv("GOVERNANCE_AUDIT_SECRET", "x" * 64)

    def test_dev_mode_allows_unauthenticated_access(self, _mock_env: None) -> None:
        """DEV_MODE=true should allow access without any credentials."""
        # Reload the module to pick up env changes
        import importlib
        import codeatelier_governance.console.app as app_mod
        importlib.reload(app_mod)
        assert app_mod.DEV_MODE is True

    def test_health_endpoint_is_unauthenticated(self) -> None:
        """The /api/health endpoint should never require auth."""
        from codeatelier_governance.console.app import app
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("httpx not installed")
        # Health endpoint works regardless of auth state
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/health")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
