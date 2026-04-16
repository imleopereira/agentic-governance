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
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from codeatelier_governance.audit.sanitization import sanitize_metadata, sanitize_string

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
    model: str | None = Field(default=None, max_length=128)
    input_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    output_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        # BLOCKER C5: validate raw metadata size FIRST so an attacker
        # cannot bypass the 64 KiB DoS cap by submitting one giant string
        # that the sanitizer would otherwise truncate. THEN sanitize for
        # ANSI / C0 control chars on every string leaf.
        _validate_metadata(self.metadata)
        sanitized = sanitize_metadata(self.metadata)
        object.__setattr__(self, "metadata", sanitized)
        # S5 P1 #1: every caller-controllable string field must flow through
        # the same NFC-normalize + C0/ANSI-strip sanitizer as metadata.
        # Without this, an insider with SDK creds could forge ANSI escapes
        # into kind / agent_id / model / input_hash / output_hash. Those
        # bytes then land in AuditEventView responses, operator CLI tails,
        # and compliance exports — anywhere an audit row gets rendered.
        # Pydantic's max_length check has already run on the raw input, so
        # the sanitizer's internal cap is a belt-and-suspenders on the NFC
        # expansion case only; the field cap is the real ceiling.
        object.__setattr__(
            self, "agent_id", sanitize_string(self.agent_id, max_len=MAX_AGENT_ID_LEN)
        )
        object.__setattr__(
            self, "kind", sanitize_string(self.kind, max_len=MAX_KIND_LEN)
        )
        if self.model is not None:
            object.__setattr__(self, "model", sanitize_string(self.model, max_len=128))
        if self.input_hash is not None:
            object.__setattr__(
                self, "input_hash", sanitize_string(self.input_hash, max_len=MAX_HASH_LEN)
            )
        if self.output_hash is not None:
            object.__setattr__(
                self,
                "output_hash",
                sanitize_string(self.output_hash, max_len=MAX_HASH_LEN),
            )


PLACEHOLDER_HMAC = "0" * 64
PLACEHOLDER_METADATA_KEY = "audit.unavailable"


class AuditEventRecord(BaseModel):
    """Stored representation of an audit event including chain fields.

    Frozen — once constructed, cannot be mutated. The DB enforces append-only
    via triggers; this is the application-layer mirror of that invariant.

    A record may be a *placeholder* (returned by ``audit.log`` when both
    primary and fallback storage failed) — in that case ``is_placeholder``
    returns True. Callers who care about audit completeness should check it.
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
    model: str | None = Field(default=None, max_length=128)
    input_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    output_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    metadata: dict[str, Any]
    prev_hash: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    hmac: str = Field(min_length=64, max_length=MAX_HASH_LEN)
    created_at: datetime
    # --- F6 Track A: Ed25519 agent identity -------------------------------
    # Optional — pre-v0.6 rows and rows written with agent_identity disabled
    # carry None/unsigned here. See design doc constraints #1 and #7.
    signature: bytes | None = Field(default=None)
    signing_key_fingerprint: str | None = Field(default=None, max_length=MAX_HASH_LEN)
    signature_status: Literal[
        "signed",
        "unsigned",
        "unsigned_local_failure",
        "legacy_unsigned",
        "revoked_key",
        "invalid_signature",
        "unknown_key",
    ] = "unsigned"

    @property
    def is_placeholder(self) -> bool:
        """True iff this record was synthesized because storage was down.

        A placeholder has ``hmac == "0" * 64`` and a metadata flag
        ``audit.unavailable=True``. The host application got a record back
        so its flow continued, but the underlying audit substrate was
        unable to persist the event. Operators should alert on these.
        """
        return (
            self.hmac == PLACEHOLDER_HMAC
            and self.metadata.get(PLACEHOLDER_METADATA_KEY) is True
        )
