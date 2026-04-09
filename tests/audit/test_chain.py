"""Tests for HMAC chain construction and verification."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from uuid import uuid4


from codeatelier_governance.audit.chain import compute_event_hmac, verify_event
from codeatelier_governance.audit.models import AuditEventRecord


def _make_record(secret: bytes, **overrides: object) -> AuditEventRecord:
    fields: dict = {
        "event_id": uuid4(),
        "session_id": uuid4(),
        "agent_id": "a",
        "parent_event_id": None,
        "kind": "k",
        "input_hash": None,
        "output_hash": None,
        "metadata": {"x": 1},
        "prev_hash": None,
        "created_at": datetime.now(timezone.utc),
    }
    fields.update(overrides)
    mac = compute_event_hmac(secret=secret, **fields)
    return AuditEventRecord(hmac=mac, **fields)


def test_hmac_is_deterministic() -> None:
    secret = b"x" * 32
    eid = uuid4()
    sid = uuid4()
    ts = datetime.now(timezone.utc)
    a = compute_event_hmac(
        secret=secret,
        event_id=eid,
        session_id=sid,
        agent_id="a",
        parent_event_id=None,
        kind="k",
        input_hash=None,
        output_hash=None,
        metadata={"x": 1, "y": 2},
        prev_hash=None,
        created_at=ts,
    )
    b = compute_event_hmac(
        secret=secret,
        event_id=eid,
        session_id=sid,
        agent_id="a",
        parent_event_id=None,
        kind="k",
        input_hash=None,
        output_hash=None,
        metadata={"y": 2, "x": 1},  # different insertion order
        prev_hash=None,
        created_at=ts,
    )
    assert a == b, "HMAC must be insensitive to dict ordering"


def test_verify_clean_record() -> None:
    secret = secrets.token_bytes(32)
    record = _make_record(secret)
    assert verify_event(record, secret) is True


def test_verify_fails_on_wrong_secret() -> None:
    secret = secrets.token_bytes(32)
    record = _make_record(secret)
    assert verify_event(record, b"y" * 32) is False


def test_verify_fails_on_metadata_tamper() -> None:
    """Mutating metadata then re-wrapping in a new record breaks the chain."""
    secret = secrets.token_bytes(32)
    record = _make_record(secret, metadata={"original": True})
    # An attacker rebuilds the record with different metadata but the OLD hmac
    tampered = AuditEventRecord(
        event_id=record.event_id,
        session_id=record.session_id,
        agent_id=record.agent_id,
        parent_event_id=record.parent_event_id,
        kind=record.kind,
        input_hash=record.input_hash,
        output_hash=record.output_hash,
        metadata={"original": False},  # changed
        prev_hash=record.prev_hash,
        hmac=record.hmac,
        created_at=record.created_at,
    )
    assert verify_event(tampered, secret) is False


def test_verify_fails_on_kind_tamper() -> None:
    secret = secrets.token_bytes(32)
    record = _make_record(secret, kind="charge.started")
    tampered = AuditEventRecord(
        event_id=record.event_id,
        session_id=record.session_id,
        agent_id=record.agent_id,
        parent_event_id=record.parent_event_id,
        kind="charge.refunded",  # attacker rewrote the action
        input_hash=record.input_hash,
        output_hash=record.output_hash,
        metadata=record.metadata,
        prev_hash=record.prev_hash,
        hmac=record.hmac,
        created_at=record.created_at,
    )
    assert verify_event(tampered, secret) is False


def test_chain_link_changes_with_prev_hash() -> None:
    """Two events with the same content but different prev_hash MUST differ."""
    secret = secrets.token_bytes(32)
    eid = uuid4()
    sid = uuid4()
    ts = datetime.now(timezone.utc)
    base_kwargs = dict(
        secret=secret,
        event_id=eid,
        session_id=sid,
        agent_id="a",
        parent_event_id=None,
        kind="k",
        input_hash=None,
        output_hash=None,
        metadata={},
        created_at=ts,
    )
    a = compute_event_hmac(prev_hash=None, **base_kwargs)
    b = compute_event_hmac(prev_hash="deadbeef" * 8, **base_kwargs)
    assert a != b
