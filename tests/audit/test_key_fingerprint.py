"""F6 Track B: fingerprint construction tests.

The fingerprint MUST be salted HMAC-SHA256(key, context) — never raw
sha256(key). A silent change to this scheme invalidates every customer's
stored fingerprint and would only surface during a rotation event in
production, so a pinned test vector is mandatory.
"""
from __future__ import annotations

import hashlib
import hmac

from codeatelier_governance.audit.keys import (
    FINGERPRINT_CONTEXT,
    fingerprint_key,
)


def test_fingerprint_pinned_test_vector() -> None:
    """Test vector: key = 32 zero bytes.

    This value is pinned. If it changes, every customer's stored
    fingerprint becomes invalid. The design doc section 3 mandates this
    test. Do not update the expected hex without an explicit migration
    plan for deployed customers.
    """
    key = b"\x00" * 32
    expected = "a3792a42666f4419b66e858f7067b85c7eb9839c5fd0aafa2c342ea9d9b9a116"
    assert fingerprint_key(key) == expected


def test_fingerprint_is_not_raw_sha256() -> None:
    """Guard against regressions to raw sha256(key)."""
    key = b"\x00" * 32
    raw = hashlib.sha256(key).hexdigest()
    assert fingerprint_key(key) != raw


def test_fingerprint_salt_context_pinned() -> None:
    """The domain separation tag is pinned to v1."""
    assert FINGERPRINT_CONTEXT == b"codeatelier.fingerprint.v1"


def test_fingerprint_different_keys_differ() -> None:
    assert fingerprint_key(b"a" * 32) != fingerprint_key(b"b" * 32)


def test_fingerprint_is_deterministic() -> None:
    k = b"weak-key-do-not-use"
    assert fingerprint_key(k) == fingerprint_key(k)


def test_fingerprint_matches_manual_hmac() -> None:
    """Sanity check: the function IS HMAC(key, context)."""
    k = b"some-key-bytes"
    manual = hmac.new(k, b"codeatelier.fingerprint.v1", hashlib.sha256).hexdigest()
    assert fingerprint_key(k) == manual
