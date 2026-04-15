"""F6 Track B: overlapping-rotation correctness.

The DA flagged the realistic incident-response scenario where an
operator rotates the HMAC key TWICE in rapid succession (e.g. after
realizing the first rotation used a key the attacker also knew). This
produces two rotation markers very close together in chain_seq, with
three distinct keys and potentially tight active-window overlaps.

A single ``verify_chain_with_rotation`` call spanning both boundaries
must walk all three segments correctly and validate both markers.
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
    find_active_key_at_seq,
)
from codeatelier_governance.audit.models import AuditEventRecord


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    clear_key_cache()
    yield
    clear_key_cache()


def _row(
    *, secret: bytes, prev_hash: str | None, chain_seq: int, sid
) -> ChainVerifyRow:
    fields: dict = {
        "event_id": uuid4(),
        "session_id": sid,
        "agent_id": "a",
        "parent_event_id": None,
        "kind": "tool.call",
        "input_hash": None,
        "output_hash": None,
        "metadata": {"n": chain_seq},
        "prev_hash": prev_hash,
        "created_at": datetime.now(timezone.utc),
    }
    mac = compute_event_hmac(secret=secret, **fields)
    return ChainVerifyRow(
        chain_seq=chain_seq,
        record=AuditEventRecord(hmac=mac, **fields),
        hmac_next=None,
    )


def _marker(
    *, outgoing: bytes, incoming: bytes, prev_hash: str | None, chain_seq: int, sid
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
        session_id=sid,
        agent_id="gov",
        parent_event_id=None,
        metadata=meta,
        prev_hash=prev_hash,
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
        prev_hash=prev_hash,
        hmac=out_mac,
        created_at=ts,
    )
    return ChainVerifyRow(chain_seq=chain_seq, record=rec, hmac_next=in_mac)


def test_two_consecutive_rotations_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chain of 5 rows with rotations between rows 2/3 and 3/4.

    Layout (chain_seq : key):
        1 : k1
        2 : k1   (marker: outgoing k1, incoming k2)
        3 : k2   (marker: outgoing k2, incoming k3)
        4 : k3
        5 : k3
    """
    k1 = b"key-one-bytes-11111111111111111111"
    k2 = b"key-two-bytes-22222222222222222222"
    k3 = b"key-three-bytes-333333333333333333"
    fp1, fp2, fp3 = fingerprint_key(k1), fingerprint_key(k2), fingerprint_key(k3)
    monkeypatch.setenv("OV_K1", k1.decode("utf-8"))
    monkeypatch.setenv("OV_K2", k2.decode("utf-8"))
    monkeypatch.setenv("OV_K3", k3.decode("utf-8"))
    uri_map = {
        fp1: "env://OV_K1",
        fp2: "env://OV_K2",
        fp3: "env://OV_K3",
    }
    sid = uuid4()

    row1 = _row(secret=k1, prev_hash=None, chain_seq=1, sid=sid)
    marker1 = _marker(
        outgoing=k1, incoming=k2, prev_hash=row1.record.hmac, chain_seq=2, sid=sid
    )
    marker2 = _marker(
        outgoing=k2, incoming=k3, prev_hash=marker1.record.hmac, chain_seq=3, sid=sid
    )
    row4 = _row(secret=k3, prev_hash=marker2.record.hmac, chain_seq=4, sid=sid)
    row5 = _row(secret=k3, prev_hash=row4.record.hmac, chain_seq=5, sid=sid)

    # k1 retires at 3 (first seq it no longer covers). k2 activates at 3
    # and retires at 4. k3 activates at 4.
    key_versions = [
        KeyVersion(1, fp1, activated_at_chain_seq=0, retired_at_chain_seq=3),
        KeyVersion(2, fp2, activated_at_chain_seq=3, retired_at_chain_seq=4),
        KeyVersion(3, fp3, activated_at_chain_seq=4, retired_at_chain_seq=None),
    ]

    result = verify_chain_with_rotation(
        [row1, marker1, marker2, row4, row5], key_versions, uri_map
    )
    assert result.status == "ok", result
    assert result.verified == 5
    assert result.failed == 0
    assert result.unverified == 0


def test_find_active_key_picks_latest_in_overlap() -> None:
    """If two key versions overlap at a given seq, the LATEST activation wins.

    Defensive: a bugged rotation writer could leave two rows with
    overlapping [activated, retired) windows. The verifier must still
    make a deterministic, correct choice.
    """
    versions = [
        KeyVersion(1, "fp-old", activated_at_chain_seq=0, retired_at_chain_seq=10),
        KeyVersion(2, "fp-mid", activated_at_chain_seq=5, retired_at_chain_seq=15),
    ]
    result = find_active_key_at_seq(7, versions)
    assert result is not None
    assert result.fingerprint == "fp-mid"  # latest activation wins
