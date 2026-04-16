"""F6 Track B rotation edge cases (DA findings batch)."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from codeatelier_governance.audit.chain import (
    ChainVerifyRow,
    ChainVerifyResult,
    KEY_ROTATION_KIND,
    RotationMarkerRow,
    compute_rotation_marker_macs,
    find_active_key_at_seq,
    verify_chain_with_rotation,
    verify_rotation_marker,
)
from codeatelier_governance.audit.keys import (
    KeyVersion,
    clear_key_cache,
    fingerprint_key,
)
from codeatelier_governance.audit.models import AuditEventRecord


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    clear_key_cache()
    yield
    clear_key_cache()


# --- Edge case #15 ----------------------------------------------------------
def test_rotation_marker_with_outgoing_only_mac_rejected() -> None:
    """A marker that verifies under the outgoing key but NOT the
    incoming key must be rejected — the dual-signature requirement."""
    outgoing = b"o" * 32
    incoming = b"i" * 32
    sid = uuid4()
    eid = uuid4()
    ts = datetime.now(timezone.utc)
    out_mac, in_mac = compute_rotation_marker_macs(
        outgoing_secret=outgoing,
        incoming_secret=incoming,
        event_id=eid,
        session_id=sid,
        agent_id="gov",
        parent_event_id=None,
        metadata={"x": 1},
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
        metadata={"x": 1},
        prev_hash=None,
        hmac=out_mac,
        created_at=ts,
    )
    # Tamper with hmac_next: right outgoing MAC, bogus incoming MAC.
    bad_marker = RotationMarkerRow(
        record=rec,
        hmac_next="0" * 64,
        outgoing_fingerprint=fingerprint_key(outgoing),
        incoming_fingerprint=fingerprint_key(incoming),
    )
    assert not verify_rotation_marker(
        bad_marker, outgoing_secret=outgoing, incoming_secret=incoming,
    )

    # Sanity check: a correctly-dual-signed marker passes.
    good_marker = RotationMarkerRow(
        record=rec,
        hmac_next=in_mac,
        outgoing_fingerprint=fingerprint_key(outgoing),
        incoming_fingerprint=fingerprint_key(incoming),
    )
    assert verify_rotation_marker(
        good_marker, outgoing_secret=outgoing, incoming_secret=incoming,
    )


# --- Edge case #16 ----------------------------------------------------------
def test_verify_chain_with_rotation_empty_row_list() -> None:
    """Empty input must return ok / 0 / 0 / 0 without error."""
    result = verify_chain_with_rotation([], [], {})
    assert isinstance(result, ChainVerifyResult)
    assert result.verified == 0
    assert result.failed == 0
    assert result.unverified == 0
    assert result.status == "ok"


# --- Edge case #17 ----------------------------------------------------------
def test_find_active_key_at_seq_two_keys_overlap_returns_latest() -> None:
    """When two keys both cover a given seq, the latest-activated wins.

    This pins the overlapping-rotation rule from the DA blocker fix.
    """
    a = KeyVersion(
        key_version=1, fingerprint="fp-a",
        activated_at_chain_seq=50, retired_at_chain_seq=200,
    )
    b = KeyVersion(
        key_version=2, fingerprint="fp-b",
        activated_at_chain_seq=90, retired_at_chain_seq=150,
    )
    # At seq=100, both key versions are in their active window.
    winner = find_active_key_at_seq(100, [a, b])
    assert winner is not None
    assert winner.fingerprint == "fp-b"  # latest activation wins


# --- Edge case #18 ----------------------------------------------------------
def test_rotation_at_chain_seq_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rotation marker at chain_seq=0 (immediately after session start):
    outgoing activated_at=0, retired_at=1; new activated_at=1.
    Verifies cleanly — boundary edge."""
    outgoing = b"old-key-bytes-aaaaaaaaaaaaaaaaaa"
    incoming = b"new-key-bytes-bbbbbbbbbbbbbbbbbb"
    monkeypatch.setenv("T18_OLD", outgoing.decode("utf-8"))
    monkeypatch.setenv("T18_NEW", incoming.decode("utf-8"))
    old_fp = fingerprint_key(outgoing)
    new_fp = fingerprint_key(incoming)
    uri_map = {old_fp: "env://T18_OLD", new_fp: "env://T18_NEW"}

    sid = uuid4()
    eid = uuid4()
    ts = datetime.now(timezone.utc)
    out_mac, in_mac = compute_rotation_marker_macs(
        outgoing_secret=outgoing,
        incoming_secret=incoming,
        event_id=eid,
        session_id=sid,
        agent_id="gov",
        parent_event_id=None,
        metadata={},
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
        metadata={},
        prev_hash=None,
        hmac=out_mac,
        created_at=ts,
    )
    marker = ChainVerifyRow(chain_seq=0, record=rec, hmac_next=in_mac)
    kv = [
        KeyVersion(1, old_fp, activated_at_chain_seq=0, retired_at_chain_seq=1),
        KeyVersion(2, new_fp, activated_at_chain_seq=1, retired_at_chain_seq=None),
    ]
    result = verify_chain_with_rotation([marker], kv, uri_map)
    assert result.status == "ok", result
    assert result.verified == 1
