"""Pydantic models for audit events.

Two models:
    AuditEvent       — what callers construct and pass to sdk.audit.log()
    AuditEventRecord — what the SDK stores (adds event_id, prev_hash, hmac, created_at)

Both use strict mode and explicit size caps to prevent oversized payloads from
becoming a DoS vector against the audit store.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# --- Size caps (security: DoS prevention at the SDK boundary) -----------------
MAX_KIND_LEN = 128
MAX_HASH_LEN = 128
MAX_AGENT_ID_LEN = 256
MAX_METADATA_KEYS = 64
MAX_METADATA_VALUE_BYTES = 8 * 1024  # 8 KiB per value
MAX_METADATA_TOTAL_BYTES = 64 * 1024  # 64 KiB total per event


class EventKind(str, Enum):
    """Canonical audit event kinds.

    Callers may pass any string for ``AuditEvent.kind``; this enum is for
    convenience and consistency. Custom kinds should namespace with a dot
    (e.g. ``my_app.invoice.approved``).
    """

    AGENT_START = "agent.start"
    AGENT_END = "agent.end"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    LLM_CALL = "llm.call"
    LLM_RESULT = "llm.result"
    SCOPE_VIOLATION = "scope.violation"
    BUDGET_EXCEEDED = "budget.exceeded"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    CUSTOM = "custom"


def _validate_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Reject oversized or pathological metadata payloads early."""
    if len(value) > MAX_METADATA_KEYS:
        raise ValueError(
            f"audit metadata: too many keys ({len(value)} > {MAX_METADATA_KEYS}). "
            f"Fix: split the event or move large data to input_hash/output_hash."
        )
    import json

    try:
        encoded = json.dumps(value, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"audit metadata: not JSON-serializable ({exc}). "
            f"Fix: pass only primitives (str/int/float/bool/None/list/dict)."
        ) from exc
    if len(encoded) > MAX_METADATA_TOTAL_BYTES:
        raise ValueError(
            f"audit metadata: total size {len(encoded)} bytes exceeds "
            f"cap of {MAX_METADATA_TOTAL_BYTES}. "
            f"Fix: hash large payloads and put the hash in input_hash/output_hash."
        )
    return value


class AuditEvent(BaseModel):
    """Input model for logging an audit event.

    The SDK fills in event_id, prev_hash, hmac, and created_at when storing.
    Pass session_id explicitly or use ``sdk.audit.session()`` context manager.
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    session_id: UUID | None = None
    agent_id: str = Field(min_length=1, max_length=MAX_AGENT_ID_LEN)
    parent_event_id: UUID | None = None
    kind: str = Field(min_length=1, max_length=MAX_KIND_LEN)
    input_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    output_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        _validate_metadata(self.metadata)


class AuditEventRecord(BaseModel):
    """Stored representation of an audit event including chain fields.

    Frozen — once constructed, cannot be mutated. The DB enforces append-only
    via triggers; this is the application-layer mirror of that invariant.
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
    )

    event_id: UUID
    session_id: UUID
    agent_id: str = Field(min_length=1, max_length=MAX_AGENT_ID_LEN)
    parent_event_id: UUID | None
    kind: str = Field(min_length=1, max_length=MAX_KIND_LEN)
    input_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    output_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    metadata: dict[str, Any]
    prev_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    hmac: str = Field(min_length=64, max_length=MAX_HASH_LEN)
    created_at: datetime
