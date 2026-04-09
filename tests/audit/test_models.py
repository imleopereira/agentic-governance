"""Tests for AuditEvent / AuditEventRecord validation."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from codeatelier_governance.audit.models import (
    MAX_AGENT_ID_LEN,
    MAX_METADATA_KEYS,
    MAX_METADATA_TOTAL_BYTES,
    AuditEvent,
    AuditEventRecord,
)


def test_audit_event_minimum_valid() -> None:
    event = AuditEvent(agent_id="agent-1", kind="tool.call")
    assert event.agent_id == "agent-1"
    assert event.kind == "tool.call"
    assert event.metadata == {}
    assert event.session_id is None
    assert event.parent_event_id is None


def test_audit_event_rejects_empty_agent_id() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(agent_id="", kind="tool.call")


def test_audit_event_rejects_empty_kind() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(agent_id="a", kind="")


def test_audit_event_rejects_oversized_agent_id() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(agent_id="x" * (MAX_AGENT_ID_LEN + 1), kind="k")


def test_audit_event_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(agent_id="a", kind="k", surprise="boom")  # type: ignore[call-arg]


def test_audit_event_rejects_too_many_metadata_keys() -> None:
    big = {f"k{i}": i for i in range(MAX_METADATA_KEYS + 1)}
    with pytest.raises(ValidationError):
        AuditEvent(agent_id="a", kind="k", metadata=big)


def test_audit_event_rejects_oversized_metadata() -> None:
    huge = {"data": "x" * (MAX_METADATA_TOTAL_BYTES + 100)}
    with pytest.raises(ValidationError):
        AuditEvent(agent_id="a", kind="k", metadata=huge)


def test_audit_event_record_is_frozen() -> None:
    rec = AuditEventRecord(
        event_id=uuid4(),
        session_id=uuid4(),
        agent_id="a",
        parent_event_id=None,
        kind="k",
        input_hash=None,
        output_hash=None,
        metadata={},
        prev_hash=None,
        hmac="a" * 64,
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValidationError):
        rec.agent_id = "tampered"  # type: ignore[misc]


def test_audit_event_strict_types() -> None:
    # Strict mode should reject coercion of int → str
    with pytest.raises(ValidationError):
        AuditEvent(agent_id=123, kind="k")  # type: ignore[arg-type]
