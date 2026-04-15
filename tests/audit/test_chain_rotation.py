"""F6 Track B: rotation-aware chain verification tests.

Pins:
  * off-by-one boundary rule (marker row is LAST of old segment)
  * dual-signature requirement (single-key forgery rejected)
  * missing key → status 'unverified', not 'failed'
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from codeatelier_governance.audit.chain import (
    ChainVerifyRow,
    KEY_ROTATION_KIND,
    compute_event_hmac,
    compute_rotation_marker_macs,
    verify_chain_with_rotation,
)
from codeatelier_governance.audit.keys import (
    KeyVersion,
    clear_key_cache,
    fingerprint_key,
)
from codeatelier_governance.audit.models import AuditEventRecord


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    clear_key_cache()
    yield
    clear_key_cache()


def _make_row(
    *,
    secret: bytes,
    prev_hash: str | None,
    chain_seq: int,
    kind: str = "tool.call",
    session_id=None,
) -> ChainVerifyRow:
    fields: dict = {
        "event_id": uuid4(),
        "session_id": session_id or uuid4(),
        "agent_id": "a",
        "parent_event_id": None,
        "kind": kind,
        "input_hash": None,
        "output_hash": None,
        "metadata": {"n": chain_seq},
        "prev_hash": prev_hash,
        "created_at": datetime.now(timezone.utc),
    }
    mac = compute_event_hmac(secret=secret, **fields)
    rec = AuditEventRecord(hmac=mac, **fields)
    return ChainVerifyRow(chain_seq=chain_seq, record=rec, hmac_next=None)


def _make_marker(
    *,
    outgoing: bytes,
    incoming: bytes,
    prev_hash: str | None,
    chain_seq: int,
    session_id,
) -> ChainVerifyRow:
    eid = uuid4()
    ts = datetime.now(timezone.utc)
    meta = {
        "outgoing_fingerprint": fingerprint_key(outgoing),
        "incoming_fingerprint": fingerprint_key(incoming),
    }
    out_mac, in_mac = compute_rotation_marker_macs(
        outgoing_secret=outgoing,
        incoming_secret=incoming,
        event_id=eid,
        session_id=session_id,
        agent_id="gov",
        parent_event_id=None,
        metadata=meta,
        prev_hash=prev_hash,
        created_at=ts,
    )
    rec = AuditEventRecord(
        event_id=eid,
        session_id=session_id,
        agent_id="gov",
        parent_event_id=None,
        kind=KEY_ROTATION_KIND,
        input_hash=None,
        output_hash=None,
        metadata=meta,
        prev_hash=prev_hash,
        hmac=out_mac,
        created_at=ts,
    )
    return ChainVerifyRow(chain_seq=chain_seq, record=rec, hmac_next=in_mac)


def test_single_rotation_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chain: row1 (old) -> marker@2 -> row3 (new). All verify."""
    old = b"old-key-bytes-000000000000000001"
    new = b"new-key-bytes-000000000000000002"
    sid = uuid4()
    old_fp = fingerprint_key(old)
    new_fp = fingerprint_key(new)

    monkeypatch.setenv("TEST_OLD_K", old.decode("utf-8"))
    monkeypatch.setenv("TEST_NEW_K", new.decode("utf-8"))
    uri_map = {old_fp: "env://TEST_OLD_K", new_fp: "env://TEST_NEW_K"}

    row1 = _make_row(secret=old, prev_hash=None, chain_seq=1, session_id=sid)
    marker = _make_marker(
        outgoing=old,
        incoming=new,
        prev_hash=row1.record.hmac,
        chain_seq=2,
        session_id=sid,
    )
    row3 = _make_row(
        secret=new, prev_hash=marker.record.hmac, chain_seq=3, session_id=sid
    )

    key_versions = [
        KeyVersion(
            key_version=1,
            fingerprint=old_fp,
            activated_at_chain_seq=0,
            retired_at_chain_seq=3,
        ),
        KeyVersion(
            key_version=2,
            fingerprint=new_fp,
            activated_at_chain_seq=3,
            retired_at_chain_seq=None,
        ),
    ]

    result = verify_chain_with_rotation([row1, marker, row3], key_versions, uri_map)
    assert result.status == "ok", result
    assert result.verified == 3
    assert result.failed == 0
    assert result.unverified == 0


def test_marker_row_uses_outgoing_key_not_incoming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker row itself lives in the OUTGOING segment.

    This pins the off-by-one rule: marker.chain_seq is the LAST row of
    the old segment, not the first of the new.
    """
    old = b"old-key-bytes-xxxxxxxxxxxxxxxxxx"
    new = b"new-key-bytes-yyyyyyyyyyyyyyyyyy"
    sid = uuid4()
    old_fp = fingerprint_key(old)
    new_fp = fingerprint_key(new)
    monkeypatch.setenv("T_OLD", old.decode("utf-8"))
    monkeypatch.setenv("T_NEW", new.decode("utf-8"))
    uri_map = {old_fp: "env://T_OLD", new_fp: "env://T_NEW"}

    # Off-by-one invariant: marker at chain_seq=N is the LAST row of the
    # OUTGOING segment. Therefore old.retired_at_chain_seq = N+1 (first
    # seq the old key does NOT authenticate) and new.activated_at = N+1.
    marker = _make_marker(
        outgoing=old, incoming=new, prev_hash=None, chain_seq=1, session_id=sid
    )
    key_versions = [
        KeyVersion(1, old_fp, activated_at_chain_seq=0, retired_at_chain_seq=2),
        KeyVersion(2, new_fp, activated_at_chain_seq=2, retired_at_chain_seq=None),
    ]
    result = verify_chain_with_rotation([marker], key_versions, uri_map)
    assert result.status == "ok"
    assert result.verified == 1


def test_forged_marker_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker signed only under the outgoing key must NOT verify.

    Simulates an attacker who exfiltrates the outgoing key and forges a
    rotation row. Dual-signature requirement kills the attack.
    """
    old = b"out-key-bytes-" + b"0" * 18
    real_new = b"real-new-key-" + b"1" * 19
    attacker_new = b"attacker-new-" + b"2" * 19
    sid = uuid4()
    old_fp = fingerprint_key(old)
    new_fp = fingerprint_key(real_new)

    monkeypatch.setenv("F_OLD", old.decode("utf-8"))
    monkeypatch.setenv("F_NEW", real_new.decode("utf-8"))
    uri_map = {old_fp: "env://F_OLD", new_fp: "env://F_NEW"}

    # Build a marker where hmac_next was computed under attacker_new,
    # but metadata claims incoming = real new fingerprint. The verifier
    # will resolve real_new bytes and recompute the incoming MAC; the
    # stored hmac_next (under attacker_new) will NOT match.
    eid = uuid4()
    ts = datetime.now(timezone.utc)
    meta = {"outgoing_fingerprint": old_fp, "incoming_fingerprint": new_fp}
    out_mac, fake_in_mac = compute_rotation_marker_macs(
        outgoing_secret=old,
        incoming_secret=attacker_new,  # forged
        event_id=eid,
        session_id=sid,
        agent_id="gov",
        parent_event_id=None,
        metadata=meta,
        prev_hash=None,
        created_at=ts,
    )
    rec = AuditEventRecord(
        event_id=eid,
        session_id=sid,
        agent_id="gov",
        parent_event_id=None,
        kind=KEY_ROTATION_KIND,
        input_hash=None,
        output_hash=None,
        metadata=meta,
        prev_hash=None,
        hmac=out_mac,
        created_at=ts,
    )
    marker = ChainVerifyRow(chain_seq=1, record=rec, hmac_next=fake_in_mac)
    key_versions = [
        KeyVersion(1, old_fp, 0, 2),
        KeyVersion(2, new_fp, 2, None),
    ]
    result = verify_chain_with_rotation([marker], key_versions, uri_map)
    assert result.status == "failed"
    assert result.failed == 1


def test_missing_key_returns_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing env var → status 'unverified', not 'failed'."""
    k = b"present-key-bytes-" + b"0" * 14
    fp = fingerprint_key(k)
    sid = uuid4()
    # Note: uri_map empty — we know the fingerprint but can't resolve it.
    row = _make_row(secret=k, prev_hash=None, chain_seq=1, session_id=sid)
    key_versions = [KeyVersion(1, fp, 0, None)]
    result = verify_chain_with_rotation([row], key_versions, {})
    assert result.status == "unverified"
    assert result.unverified == 1
    assert result.failed == 0
    assert fp in result.unresolved_fingerprints
