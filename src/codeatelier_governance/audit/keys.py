"""F6 Track B — HMAC chain key rotation: key resolution and fingerprinting.

Fingerprints are salted HMAC-SHA256 under a fixed domain separation tag.
Raw sha256(key) is NOT used — it would leak material usable for offline
dictionary attack against weak operator-chosen keys (see
``decisions/2026-04-15-f6-hmac-rotation-design.md`` section 3).

Key material itself never lives in Postgres. Only the salted fingerprint
does. The verifier resolves a fingerprint back to bytes through one of two
backends:

    env://VAR_NAME     base64-encoded key bytes in an environment variable
    file:///abs/path   0600 file containing raw key bytes

Resolution is cached with a bounded LRU (max 64 entries) to defend against
pathological chains that reference many distinct fingerprints in a single
verify call — see the DA blocker fix in the design doc section 8.
"""
from __future__ import annotations

import base64
import enum
import functools
import hashlib
import hmac as _hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import structlog

_logger = structlog.get_logger(__name__)


# Domain separation tag for the fingerprint construction. The v1 suffix
# reserves the ability to migrate the fingerprint scheme without ambiguity.
FINGERPRINT_CONTEXT = b"codeatelier.fingerprint.v1"

# Bounded LRU cap. Documented in the design doc section 8: a realistic
# verifier sees O(dozen) fingerprints per year of audit data, so 64 is
# generous for real usage but small enough to defend against pathological
# unbounded cache growth from crafted chain rows.
_LRU_MAX_ENTRIES = 64


def fingerprint_key(key_bytes: bytes) -> str:
    """Salted fingerprint — NOT raw sha256.

    The salt context is a fixed domain separation tag to defeat offline
    rainbow-table attacks against weak operator-chosen keys. Because the
    construction is HMAC keyed by the secret itself, there is no shortcut
    past brute-forcing the key directly.

    Test vector (pinned in tests/audit/test_key_fingerprint.py):
        key      = b"\\x00" * 32
        expected =
            "a3792a42666f4419b66e858f7067b85c7eb9839c5fd0aafa2c342ea9d9b9a116"
    """
    return _hmac.new(key_bytes, FINGERPRINT_CONTEXT, hashlib.sha256).hexdigest()


class KeyResolution(enum.Enum):
    """Outcome states for fingerprint -> key material resolution."""

    RESOLVED = "resolved"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ResolvedKey:
    """A resolved (or unresolvable) chain key."""

    fingerprint: str
    status: KeyResolution
    material: bytes | None  # None iff status == UNAVAILABLE


@dataclass(frozen=True)
class KeyVersion:
    """A row from governance_audit_chain_keys."""

    key_version: int
    fingerprint: str
    activated_at_chain_seq: int
    retired_at_chain_seq: int | None


def _resolve_uri(uri: str) -> bytes | None:
    """Resolve a key-material URI to raw bytes, or None if unavailable.

    Supports ``env://VAR_NAME`` (base64-encoded) and ``file:///abs/path``.
    """
    if uri.startswith("env://"):
        var_name = uri[len("env://") :]
        raw = os.environ.get(var_name)
        if not raw:
            return None
        try:
            return base64.b64decode(raw, validate=True)
        except (ValueError, Exception):
            # Allow raw (non-base64) env vars too — many operators set the
            # key as literal bytes. Fall back to UTF-8 encoding.
            return raw.encode("utf-8")
    if uri.startswith("file://"):
        path = uri[len("file://") :]
        p = Path(path)
        if not p.exists():
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None
    return None


@functools.lru_cache(maxsize=_LRU_MAX_ENTRIES)
def _cached_resolve(fingerprint: str, uri: str) -> bytes | None:
    """Cached fingerprint+URI -> bytes resolution.

    The cache is bounded at ``_LRU_MAX_ENTRIES`` (64). This is an explicit
    non-unbounded cap to defend against pathological chains referencing
    many distinct fingerprints. Negative results (None) are also cached so
    a missing env var is not re-read per row.

    **Operator footgun — negative results survive env provisioning.**
    If an operator starts the process with a missing env var, resolution
    returns ``UNAVAILABLE`` and that negative result is cached under
    ``(fingerprint, uri)``. Setting the env var afterwards does NOT
    invalidate the cache: every subsequent ``resolve_key`` call will keep
    returning ``UNAVAILABLE`` until the process restarts OR
    :func:`clear_key_cache` is called explicitly. The ``rotate-chain-key``
    CLI command calls ``clear_key_cache()`` on success so a freshly
    provisioned key is resolved on the next verify pass. Any other
    deployment tooling that repairs env vars at runtime should do the
    same.

    The cache key intentionally includes the URI so that rebinding
    ``env://NAME`` to a new backing value requires calling
    ``clear_key_cache()``; otherwise a mid-rotation verifier could see a
    stale cached value.
    """
    material = _resolve_uri(uri)
    if material is None:
        return None
    # Defense in depth: validate the URI resolved to a key whose fingerprint
    # actually matches. If the operator pointed the wrong env var at a
    # fingerprint slot, refuse it — "unavailable" is the right answer, not
    # "resolved to wrong bytes".
    if fingerprint_key(material) != fingerprint:
        _logger.warning(
            "audit.keys.fingerprint_mismatch",
            expected_fingerprint=fingerprint[:16] + "...",
            uri_prefix=uri.split("://")[0],
        )
        return None
    return material


def resolve_key(fingerprint: str, uri_map: Mapping[str, str]) -> ResolvedKey:
    """Resolve a fingerprint to key material via the URI map.

    ``uri_map`` maps ``fingerprint -> "env://NAME"`` or ``"file:///path"``.
    Typical construction: the operator sets
    ``GOVERNANCE_CHAIN_KEY_<fingerprint_prefix>=env://OLD_HMAC_KEY`` and the
    verifier bootstrap turns that into a map.
    """
    uri = uri_map.get(fingerprint)
    if not uri:
        _logger.warning(
            "audit.keys.no_uri_for_fingerprint",
            fingerprint_prefix=fingerprint[:16] + "...",
        )
        return ResolvedKey(
            fingerprint=fingerprint, status=KeyResolution.UNAVAILABLE, material=None
        )
    material = _cached_resolve(fingerprint, uri)
    if material is None:
        _logger.warning(
            "audit.keys.uri_unresolvable",
            fingerprint_prefix=fingerprint[:16] + "...",
            uri_prefix=uri.split("://")[0],
        )
        return ResolvedKey(
            fingerprint=fingerprint, status=KeyResolution.UNAVAILABLE, material=None
        )
    return ResolvedKey(
        fingerprint=fingerprint, status=KeyResolution.RESOLVED, material=material
    )


def clear_key_cache() -> None:
    """Clear the bounded LRU. Used after rotation and in tests."""
    _cached_resolve.cache_clear()


def cache_info() -> functools._CacheInfo:
    """Inspect the LRU state. Tests use this to assert bounded behavior."""
    return _cached_resolve.cache_info()


def find_active_key_at_seq(
    chain_seq: int, key_versions: list[KeyVersion]
) -> KeyVersion | None:
    """Return the key version whose activation window contains ``chain_seq``.

    Overlapping-rotation rule (DA blocker fix, design doc section 7): if
    two rotations happen in rapid succession, several rows may sit inside
    overlapping [activated, retired) windows at the boundary. The correct
    selection is the LATEST key whose activation precedes ``chain_seq`` AND
    whose retirement (if any) is strictly greater than ``chain_seq``.

    Precondition: ``key_versions`` is the full history, not pre-filtered.
    """
    candidates = [
        kv
        for kv in key_versions
        if kv.activated_at_chain_seq <= chain_seq
        and (kv.retired_at_chain_seq is None or kv.retired_at_chain_seq > chain_seq)
    ]
    if not candidates:
        return None
    # Latest activation wins for overlapping windows.
    return max(candidates, key=lambda kv: kv.activated_at_chain_seq)
