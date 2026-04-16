"""Secret redaction on OTel exporter attribute flattening (v0.6.1 S5 fix).

Threat: a host that opts into ``codeatelier-governance[otel]`` and logs an
audit event whose ``metadata`` contains a secret-shaped substring (typical
accident: a stack trace, an error message, a vendor DSN) would previously
see that raw substring leave the audit substrate over the host's OTel pipe
to whichever exporter they configured (Datadog, Honeycomb, New Relic, ...).

``sanitize_metadata`` already runs upstream (NFC normalisation, ANSI strip,
size caps) but is a SHAPE control and does not know what a secret LOOKS
like. :func:`redact_secrets` is a CONTENT control; this test locks in that
``OTelExporter._build_attributes`` calls it on ``record.metadata`` before
flattening.

The test walks :func:`OTelExporter._build_attributes` directly so it does
not need the optional ``[otel]`` extra to be installed — ``_build_attributes``
is a pure ``@staticmethod`` with no OTel API contact.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from codeatelier_governance.audit.models import AuditEventRecord
from codeatelier_governance.audit.otel_exporter import GEN_AI_NS, OTelExporter


# ---------------------------------------------------------------------------
# Fixtures — all secret-shaped strings below are alphabet/AWS-example
# placeholders, not real credentials. Keep the ``ggignore`` annotations so
# GitGuardian does not false-positive on this file.
# ---------------------------------------------------------------------------
# ggignore-block
_SK_ANT = "sk-ant-api03-abcdefghijklmnopqrstuv"
_SK_PROJ = "sk-proj-abcdefghijklmnopqrstuvwx1234"
_XOXB = "xoxb-1234567890-abcdefghijklmnop"
_GHP = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
_AKIA = "AKIAIOSFODNN7EXAMPLE"
_AWS_SECRET = 'aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY1"'

_ALL_SIX_PREFIXES = (
    "sk-ant-api03",
    "sk-proj-",
    "xoxb-1234567890",
    "ghp_abcdef",
    "AKIAIOSFODNN7EXAMPLE",
    "wJalrXUtnFEMI",
)


def _make_record(metadata: dict[str, Any]) -> AuditEventRecord:
    """Build a minimum-viable AuditEventRecord for _build_attributes."""
    return AuditEventRecord(
        event_id=uuid4(),
        session_id=uuid4(),
        agent_id="test-agent",
        parent_event_id=None,
        kind="tool.call",
        model=None,
        input_hash=None,
        output_hash=None,
        metadata=metadata,
        prev_hash=None,
        hmac="a" * 64,
        created_at=datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
    )


def _flat_values(attrs: dict[str, Any]) -> list[str]:
    """Return every attribute value rendered as a string, for leak-grep."""
    return [str(v) for v in attrs.values()]


# ---------------------------------------------------------------------------
# Test 1: all six patterns at the top level are redacted
# ---------------------------------------------------------------------------
def test_all_six_patterns_redacted_from_flat_metadata() -> None:
    metadata = {
        "anthropic_key": _SK_ANT,
        "openai_key": _SK_PROJ,
        "slack_token": _XOXB,
        "github_token": _GHP,
        "aws_access_key": _AKIA,
        "aws_secret": _AWS_SECRET,
    }
    record = _make_record(metadata)

    attrs = OTelExporter._build_attributes(record)

    # Every raw secret prefix should be absent from every attribute value.
    joined = "\n".join(_flat_values(attrs))
    for marker in _ALL_SIX_PREFIXES:
        assert marker not in joined, f"{marker} leaked through OTel attributes"
    # Each key is still present (redaction replaces the value, not the key).
    for key in metadata:
        assert f"{GEN_AI_NS}.metadata.{key}" in attrs
        assert "[REDACTED]" in str(attrs[f"{GEN_AI_NS}.metadata.{key}"])


# ---------------------------------------------------------------------------
# Test 2: non-secret values survive unchanged
# ---------------------------------------------------------------------------
def test_non_secret_values_survive_unchanged() -> None:
    metadata = {
        "model": "claude-sonnet-4-6",
        "tokens_in": 120,
        "tokens_out": 480,
        "ok": True,
        "ratio": 0.125,
        "note": "hello world, no secrets here",
    }
    record = _make_record(metadata)

    attrs = OTelExporter._build_attributes(record)

    assert attrs[f"{GEN_AI_NS}.metadata.model"] == "claude-sonnet-4-6"
    assert attrs[f"{GEN_AI_NS}.metadata.tokens_in"] == 120
    assert attrs[f"{GEN_AI_NS}.metadata.tokens_out"] == 480
    assert attrs[f"{GEN_AI_NS}.metadata.ok"] is True
    assert attrs[f"{GEN_AI_NS}.metadata.ratio"] == 0.125
    assert attrs[f"{GEN_AI_NS}.metadata.note"] == "hello world, no secrets here"


# ---------------------------------------------------------------------------
# Test 3: nested dict — secret buried inside an "error.trace" shape is redacted
# ---------------------------------------------------------------------------
def test_nested_dict_metadata_redacts_recursively() -> None:
    metadata = {
        "error": {
            "trace": f"Authorization: Bearer {_SK_ANT}",
            "where": "openai_client.py:42",
        },
        "retries": 3,
    }
    record = _make_record(metadata)

    attrs = OTelExporter._build_attributes(record)

    # The exporter str()s nested dicts into a single attribute value. The
    # serialised form must not contain the raw secret prefix.
    nested = str(attrs[f"{GEN_AI_NS}.metadata.error"])
    assert "sk-ant-api03" not in nested, "nested secret leaked through str() flatten"
    assert "[REDACTED]" in nested
    # Non-secret nested fields are preserved.
    assert "openai_client.py:42" in nested
    # Non-nested primitive siblings are untouched.
    assert attrs[f"{GEN_AI_NS}.metadata.retries"] == 3


# ---------------------------------------------------------------------------
# Test 4: list of secrets — every item redacted, shape preserved
# ---------------------------------------------------------------------------
def test_list_metadata_redacts_every_item() -> None:
    metadata = {
        "secrets": [_GHP, _XOXB, "not-a-secret"],
        "counts": [1, 2, 3],
    }
    record = _make_record(metadata)

    attrs = OTelExporter._build_attributes(record)

    # The exporter str()s lists into a single attribute value. The serialised
    # form must not contain any raw secret prefix from the list.
    flat = str(attrs[f"{GEN_AI_NS}.metadata.secrets"])
    assert "ghp_abcdef" not in flat
    assert "xoxb-1234567890" not in flat
    # The non-secret string survived.
    assert "not-a-secret" in flat
    # A list of pure primitives passes through unchanged.
    assert attrs[f"{GEN_AI_NS}.metadata.counts"] == "[1, 2, 3]"


# ---------------------------------------------------------------------------
# Test 5: deeply nested — dict containing a list containing a dict of secrets
# ---------------------------------------------------------------------------
def test_deeply_nested_metadata_redacts_at_every_level() -> None:
    metadata = {
        "request": {
            "headers": [
                {"name": "Authorization", "value": f"Bearer {_SK_ANT}"},
                {"name": "X-Slack", "value": _XOXB},
                {"name": "X-Benign", "value": "just a header"},
            ],
            "body": {"aws": _AWS_SECRET},
        },
    }
    record = _make_record(metadata)

    attrs = OTelExporter._build_attributes(record)

    flat = str(attrs[f"{GEN_AI_NS}.metadata.request"])
    for marker in ("sk-ant-api03", "xoxb-1234567890", "wJalrXUtnFEMI"):
        assert marker not in flat, f"{marker} leaked through deeply-nested path"
    # The benign header value is preserved at depth.
    assert "just a header" in flat


# ---------------------------------------------------------------------------
# Test 6: original metadata dict is NOT mutated by the exporter
# ---------------------------------------------------------------------------
def test_exporter_does_not_mutate_original_metadata() -> None:
    metadata = {"key": _SK_ANT, "nested": {"key": _AKIA}}
    record = _make_record(metadata)

    # Record construction on AuditEventRecord does not sanitize (unlike
    # AuditEvent). The original object graph we handed in must still hold
    # the raw secrets after _build_attributes runs — this proves redaction
    # returns a NEW structure rather than mutating caller state.
    OTelExporter._build_attributes(record)

    assert record.metadata["key"] == _SK_ANT
    assert record.metadata["nested"]["key"] == _AKIA


# ---------------------------------------------------------------------------
# Test 7: empty metadata still produces all non-metadata attrs, no exceptions
# ---------------------------------------------------------------------------
def test_empty_metadata_is_safe() -> None:
    record = _make_record({})
    attrs = OTelExporter._build_attributes(record)

    assert f"{GEN_AI_NS}.event_id" in attrs
    assert f"{GEN_AI_NS}.kind" in attrs
    assert "gen_ai.agent.id" in attrs
    # No stray metadata.* keys leaked in.
    assert not any(k.startswith(f"{GEN_AI_NS}.metadata.") for k in attrs)


# ---------------------------------------------------------------------------
# Test 8: DA follow-up — DSN with embedded credentials is redacted
# ---------------------------------------------------------------------------
def test_database_dsn_with_credentials_is_redacted() -> None:
    """v0.6.1 DA follow-up: stack traces frequently leak ``postgres://u:p@h/db``.

    The frontend ``sanitizeErrorMessage`` already strips DSNs before they
    hit the DOM — the OTel exporter must do the same before spans fly to
    Datadog/Honeycomb/etc. Pins the DSN pattern set (postgres, postgresql,
    mysql, mongodb, redis, rediss) and the highest-value embedded
    ``user:pass`` content.
    """
    metadata = {
        # ggignore-block
        "db_error": "could not connect: postgres://alice:hunter2@db.internal:5432/prod",
        "cache_error": "ETIMEDOUT redis://cache:6379/0",
        "mongo_error": "MongoServerError mongodb://root:rootpw@mongo:27017/admin",
    }
    record = _make_record(metadata)

    attrs = OTelExporter._build_attributes(record)

    for key in metadata:
        rendered = str(attrs[f"{GEN_AI_NS}.metadata.{key}"])
        assert "postgres://" not in rendered
        assert "redis://" not in rendered
        assert "mongodb://" not in rendered
        assert "hunter2" not in rendered
        assert "rootpw" not in rendered
        assert "[REDACTED]" in rendered
