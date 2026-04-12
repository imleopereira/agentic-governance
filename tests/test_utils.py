"""Tests for shared utilities: normalize_db_url and sanitize_db_error."""
from __future__ import annotations

import pytest

from codeatelier_governance.utils import normalize_db_url, sanitize_db_error


class TestNormalizeDbUrl:
    """Gap #9: normalize_db_url utility."""

    def test_plain_postgresql_url(self) -> None:
        result = normalize_db_url("postgresql://user:pass@host/db")
        assert result == "postgresql+asyncpg://user:pass@host/db"

    def test_already_asyncpg_url(self) -> None:
        url = "postgresql+asyncpg://user:pass@host/db"
        assert normalize_db_url(url) == url

    def test_rejects_non_postgresql(self) -> None:
        with pytest.raises(ValueError, match="expected a postgresql://"):
            normalize_db_url("mysql://user:pass@host/db")

    def test_rejects_sqlite(self) -> None:
        with pytest.raises(ValueError, match="expected a postgresql://"):
            normalize_db_url("sqlite:///test.db")

    def test_component_in_error(self) -> None:
        with pytest.raises(ValueError, match="my component"):
            normalize_db_url("redis://localhost", component="my component")

    def test_preserves_full_url(self) -> None:
        url = "postgresql://admin:s3cret@db.example.com:5432/govdb?sslmode=require"
        result = normalize_db_url(url)
        assert result == "postgresql+asyncpg://admin:s3cret@db.example.com:5432/govdb?sslmode=require"


class TestSanitizeDbError:
    """Gap #10: sanitize_db_error strips URLs and SQL."""

    def test_strips_postgresql_url(self) -> None:
        exc = RuntimeError("Connection to postgresql://admin:pass@host/db failed")
        result = sanitize_db_error(exc)
        assert "postgresql://" not in result
        assert result == "RuntimeError"

    def test_strips_asyncpg_url(self) -> None:
        exc = RuntimeError("Error at postgresql+asyncpg://u:p@h/d")
        result = sanitize_db_error(exc)
        assert "postgresql" not in result

    def test_strips_sql_keywords(self) -> None:
        exc = RuntimeError("SELECT * FROM governance_audit_events WHERE 1=1")
        result = sanitize_db_error(exc)
        assert "SELECT" not in result
        assert result == "RuntimeError"

    def test_safe_message_passes_through(self) -> None:
        exc = ValueError("timeout after 5s")
        result = sanitize_db_error(exc)
        assert result == "ValueError: timeout after 5s"

    def test_long_message_truncated(self) -> None:
        exc = RuntimeError("x" * 300)
        result = sanitize_db_error(exc)
        assert result.endswith("...")
        assert len(result) < 250

    def test_type_name_preserved(self) -> None:
        exc = ConnectionError("something failed")
        result = sanitize_db_error(exc)
        assert result.startswith("ConnectionError")
