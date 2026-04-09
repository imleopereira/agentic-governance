"""HMAC chain logic for tamper-evident audit logs.

Each stored event carries an HMAC-SHA256 over a canonical serialization of
its immutable fields PLUS the previous event's HMAC in the same session.
This forms a tamper-evident chain: modifying any byte of any past event
breaks the HMAC of that event AND every event that follows it.

Verification is constant-time via ``hmac.compare_digest``.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from .models import AuditEventRecord


def _canonical(value: Any) -> str:
    """Deterministic JSON serialization for hashing.

    sort_keys ensures the same dict produces the same bytes regardless of
    insertion order; separators strips whitespace; default=str handles
    UUID/datetime/etc.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_event_hmac(
    *,
    secret: bytes,
    event_id: UUID,
    session_id: UUID,
    agent_id: str,
    parent_event_id: UUID | None,
    kind: str,
    input_hash: str | None,
    output_hash: str | None,
    metadata: dict[str, Any],
    prev_hash: str | None,
    created_at: datetime,
) -> str:
    """Compute the HMAC-SHA256 over an audit event's immutable fields.

    The ``prev_hash`` linkage means a tamper anywhere in the chain invalidates
    every subsequent event's HMAC, not just the tampered row.
    """
    payload = _canonical(
        {
            "event_id": str(event_id),
            "session_id": str(session_id),
            "agent_id": agent_id,
            "parent_event_id": str(parent_event_id) if parent_event_id else None,
            "kind": kind,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "metadata": metadata,
            "prev_hash": prev_hash,
            "created_at": created_at.isoformat(),
        }
    )
    return _hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_event(record: AuditEventRecord, secret: bytes) -> bool:
    """Recompute and constant-time-compare the HMAC for a stored event.

    Returns True iff the record is intact under ``secret``.
    """
    expected = compute_event_hmac(
        secret=secret,
        event_id=record.event_id,
        session_id=record.session_id,
        agent_id=record.agent_id,
        parent_event_id=record.parent_event_id,
        kind=record.kind,
        input_hash=record.input_hash,
        output_hash=record.output_hash,
        metadata=record.metadata,
        prev_hash=record.prev_hash,
        created_at=record.created_at,
    )
    return _hmac.compare_digest(expected, record.hmac)
