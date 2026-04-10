"""Tests for console/app.py — redaction, CORS validation, posture bounds."""
from __future__ import annotations

import pytest

from codeatelier_governance.console.app import _redact_metadata, _validate_cors_origins


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
