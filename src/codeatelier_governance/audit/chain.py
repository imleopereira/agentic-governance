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


def canonical_json(value: Any) -> str:
    """Deterministic JSON serialization for hashing.

    sort_keys ensures the same dict produces the same bytes regardless of
    insertion order; separators strips whitespace; default=str handles
    UUID/datetime/etc.

    v0.6.1: promoted from the previously-private ``_canonical`` name
    because the console's compliance-bundle export legitimately needs
    the same serialization contract the audit chain uses (so a bundle's
    signature/hash computed offline matches the server's). ``_canonical``
    remains as a back-compat alias; new callers MUST use ``canonical_json``.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# Back-compat aliases (keep callers inside the audit module working)
# ---------------------------------------------------------------------------
# Removed v0.7: ``_canonical`` (use ``canonical_json``)
_canonical = canonical_json


def compute_event_hmac(
    *,
    secret: bytes,
    event_id: UUID,
    session_id: UUID,
    agent_id: str,
    parent_event_id: UUID | None,
    kind: str,
    model: str | None = None,
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
            "model": model,
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
        model=record.model,
        input_hash=record.input_hash,
        output_hash=record.output_hash,
        metadata=record.metadata,
        prev_hash=record.prev_hash,
        created_at=record.created_at,
    )
    return _hmac.compare_digest(expected, record.hmac)


# =============================================================================
# === F6 Track B: HMAC chain key rotation =====================================
# =============================================================================
# The code below is additive. It MUST NOT modify ``verify_event`` or
# ``compute_event_hmac`` above — Track A (Ed25519 signing) also edits this
# file and those functions are shared.
#
# This section adds:
#   * ``KEY_ROTATION_KIND`` — the canonical kind for marker rows.
#   * ``compute_rotation_marker_macs`` — dual-MAC helper.
#   * ``verify_rotation_marker`` — accepts the marker only if BOTH MACs
#     verify under their respective keys.
#   * ``verify_chain_with_rotation`` — a wrapper over the existing
#     verification that walks a list of stored rows + a list of known key
#     versions and checks each row under the correct key.
#
# Boundary rule (design doc section 6):
#   The rotation marker row at chain_seq = N is considered the LAST row of
#   the OUTGOING segment. It verifies under the outgoing key via its
#   ``hmac`` column AND under the incoming key via its ``hmac_next``
#   column. The first post-rotation row at chain_seq = N+1 is verified
#   under the INCOMING key only.
#
# Off-by-one test: tests/audit/test_chain_rotation.py pins this boundary
# behavior. Overlapping-rotation test: tests/audit/test_overlapping_rotations.py.
# =============================================================================

from dataclasses import dataclass as _dataclass  # noqa: E402
from typing import Mapping as _Mapping  # noqa: E402

from .keys import (  # noqa: E402
    KeyResolution as _KeyResolution,
    KeyVersion as _KeyVersion,
    find_active_key_at_seq as _find_active_key_at_seq,
    resolve_key as _resolve_key,
)


KEY_ROTATION_KIND = "audit.chain_key_rotation"


@_dataclass(frozen=True)
class RotationMarkerRow:
    """A stored rotation marker. Dual-signed across outgoing/incoming keys.

    ``hmac`` is the conventional chain HMAC under the outgoing key; it
    links this row back to the previous event via ``prev_hash``.
    ``hmac_next`` is a second MAC over the SAME canonical payload,
    computed under the incoming key. It anchors the start of the new
    segment. The verifier accepts the marker only if BOTH MACs verify.
    """

    record: AuditEventRecord
    hmac_next: str
    outgoing_fingerprint: str
    incoming_fingerprint: str


def compute_rotation_marker_macs(
    *,
    outgoing_secret: bytes,
    incoming_secret: bytes,
    event_id: UUID,
    session_id: UUID,
    agent_id: str,
    parent_event_id: UUID | None,
    metadata: dict[str, Any],
    prev_hash: str | None,
    created_at: datetime,
) -> tuple[str, str]:
    """Compute the outgoing+incoming MACs for a rotation marker row.

    Both MACs cover the SAME canonical payload. Neither key alone can
    forge a marker that will validate — an attacker who stole only the
    outgoing key cannot point ``incoming_fingerprint`` at a key they
    control, because they can't produce the incoming-key MAC. This
    bounds single-key compromise blast radius to one rotation segment
    (design doc section 5: Cybersec HIGH fix).
    """
    outgoing_mac = compute_event_hmac(
        secret=outgoing_secret,
        event_id=event_id,
        session_id=session_id,
        agent_id=agent_id,
        parent_event_id=parent_event_id,
        kind=KEY_ROTATION_KIND,
        input_hash=None,
        output_hash=None,
        metadata=metadata,
        prev_hash=prev_hash,
        created_at=created_at,
    )
    incoming_mac = compute_event_hmac(
        secret=incoming_secret,
        event_id=event_id,
        session_id=session_id,
        agent_id=agent_id,
        parent_event_id=parent_event_id,
        kind=KEY_ROTATION_KIND,
        input_hash=None,
        output_hash=None,
        metadata=metadata,
        prev_hash=prev_hash,
        created_at=created_at,
    )
    return outgoing_mac, incoming_mac


def verify_rotation_marker(
    marker: RotationMarkerRow,
    *,
    outgoing_secret: bytes,
    incoming_secret: bytes,
) -> bool:
    """True iff BOTH MACs verify under their respective keys.

    A marker that verifies only under the outgoing key is REJECTED: it
    means the attacker held the outgoing key and forged a rotation to a
    key they control. The dual-signature requirement kills that attack.
    """
    expected_outgoing, expected_incoming = compute_rotation_marker_macs(
        outgoing_secret=outgoing_secret,
        incoming_secret=incoming_secret,
        event_id=marker.record.event_id,
        session_id=marker.record.session_id,
        agent_id=marker.record.agent_id,
        parent_event_id=marker.record.parent_event_id,
        metadata=marker.record.metadata,
        prev_hash=marker.record.prev_hash,
        created_at=marker.record.created_at,
    )
    out_ok = _hmac.compare_digest(expected_outgoing, marker.record.hmac)
    in_ok = _hmac.compare_digest(expected_incoming, marker.hmac_next)
    return out_ok and in_ok


@_dataclass(frozen=True)
class ChainVerifyRow:
    """A row to feed to ``verify_chain_with_rotation``.

    ``chain_seq`` is the DB-assigned append order. ``hmac_next`` is
    populated ONLY for rotation marker rows (kind == KEY_ROTATION_KIND);
    None on every other row.
    """

    chain_seq: int
    record: AuditEventRecord
    hmac_next: str | None


@_dataclass(frozen=True)
class ChainVerifyResult:
    """Outcome of a rotation-aware chain verification."""

    verified: int
    failed: int
    unverified: int
    status: str  # "ok" | "failed" | "unverified"
    unresolved_fingerprints: list[str]


def find_active_key_at_seq(
    chain_seq: int, key_versions: list[_KeyVersion]
) -> _KeyVersion | None:
    """Thin re-export so callers can import from ``audit.chain``.

    Delegates to ``audit.keys.find_active_key_at_seq``; duplicated here
    so that consumers of the chain verifier do not need a second import.
    """
    return _find_active_key_at_seq(chain_seq, key_versions)


def verify_chain_with_rotation(
    rows: list[ChainVerifyRow],
    key_versions: list[_KeyVersion],
    uri_map: _Mapping[str, str],
) -> ChainVerifyResult:
    """Verify a chain that may span one or more key rotations.

    Algorithm:
        For each row in chain_seq order:
          1. Look up the active key version at that row's chain_seq via
             ``find_active_key_at_seq``.
          2. Resolve the fingerprint to bytes via ``resolve_key``. If
             unavailable, mark the row ``unverified`` (NOT ``failed``) —
             see design doc section 9 for why the distinction matters.
          3. If the row is a rotation marker, also look up the NEXT key
             version (the one whose activated_at_chain_seq == chain_seq,
             or the row immediately after) and require dual-MAC verify.
          4. Otherwise, verify via ``verify_event`` under the resolved key.

    Returns a ``ChainVerifyResult``. ``status`` is:
        * ``"failed"``    if any row's HMAC does not verify under a
                          resolved key (tamper detected).
        * ``"unverified"`` if no failures but at least one row could not
                          be checked because its key was unresolvable.
        * ``"ok"``         if every row verified cleanly.

    This function does NOT modify the existing single-key
    ``verify_event`` — it composes on top of it. The off-by-one rule at
    rotation boundaries is encoded here, not in ``verify_event``.
    """
    verified = 0
    failed = 0
    unverified = 0
    unresolved: set[str] = set()

    # Pre-sort key versions once for deterministic lookup.
    sorted_versions = sorted(key_versions, key=lambda k: k.activated_at_chain_seq)

    for row in rows:
        active = find_active_key_at_seq(row.chain_seq, sorted_versions)
        if active is None:
            unverified += 1
            unresolved.add("<no-key-version-for-seq>")
            continue

        outgoing = _resolve_key(active.fingerprint, uri_map)
        if outgoing.status is _KeyResolution.UNAVAILABLE:
            unverified += 1
            unresolved.add(active.fingerprint)
            continue

        is_marker = row.record.kind == KEY_ROTATION_KIND and row.hmac_next is not None
        if is_marker:
            # Incoming key is the next version whose activation is exactly
            # at this marker's chain_seq OR the version active at chain_seq+1.
            incoming_version = find_active_key_at_seq(
                row.chain_seq + 1, sorted_versions
            )
            if incoming_version is None or incoming_version.fingerprint == active.fingerprint:
                # No post-boundary key known; treat as unverified rather than
                # failed — the operator may simply not have mounted it yet.
                unverified += 1
                unresolved.add("<no-incoming-key-for-marker>")
                continue
            incoming = _resolve_key(incoming_version.fingerprint, uri_map)
            if incoming.status is _KeyResolution.UNAVAILABLE:
                unverified += 1
                unresolved.add(incoming_version.fingerprint)
                continue
            if outgoing.material is None or incoming.material is None:
                # Should be unreachable: both were just resolved above.
                # Raise loudly rather than using ``assert`` (which becomes
                # a no-op under ``python -O`` on a security-critical path).
                raise RuntimeError(
                    "invariant violated: resolved key material must be "
                    "present before constructing a rotation marker"
                )
            marker = RotationMarkerRow(
                record=row.record,
                hmac_next=row.hmac_next or "",
                outgoing_fingerprint=active.fingerprint,
                incoming_fingerprint=incoming_version.fingerprint,
            )
            if verify_rotation_marker(
                marker,
                outgoing_secret=outgoing.material,
                incoming_secret=incoming.material,
            ):
                verified += 1
            else:
                failed += 1
        else:
            if outgoing.material is None:
                # Unreachable after the UNAVAILABLE guard above; raise
                # instead of ``assert`` so the invariant holds under -O.
                raise RuntimeError(
                    "invariant violated: outgoing key material must be "
                    "present at this point"
                )
            if verify_event(row.record, outgoing.material):
                verified += 1
            else:
                failed += 1

    if failed > 0:
        status = "failed"
    elif unverified > 0:
        status = "unverified"
    else:
        status = "ok"

    return ChainVerifyResult(
        verified=verified,
        failed=failed,
        unverified=unverified,
        status=status,
        unresolved_fingerprints=sorted(unresolved),
    )

# =============================================================================
# === End F6 Track B ==========================================================
# =============================================================================


# =============================================================================
# === F6 Track A: Ed25519 signing ==============================================
# =============================================================================
# These helpers are ADDITIVE to the chain module. Track B (HMAC rotation)
# owns edits to ``compute_event_hmac`` / ``verify_event`` above; Track A
# contributes only the Ed25519 sign/verify helpers below. Do NOT modify
# functions above this marker without coordinating with the other track.
#
# The helpers here are intentionally decoupled from AuditModule.log so they
# can be unit-tested in isolation. Wiring into AuditModule.log is COMPLETE
# as of v0.6.1 — see ``audit/module.py`` ``_log_unsafe`` (the signing block
# surrounding ``sign_audit_row``). Keeping the helpers separate means unit
# tests can exercise the signing contract without constructing an
# AuditModule / AuditStore pair.


def canonical_row_bytes_for_signing(
    *,
    event_id: Any,
    session_id: Any,
    agent_id: str,
    parent_event_id: Any | None,
    kind: str,
    model: str | None,
    input_hash: str | None,
    output_hash: str | None,
    metadata: dict[str, Any],
    prev_hash: str | None,
    created_at: datetime,
    hmac_hex: str,
) -> bytes:
    """Produce the canonical bytes Ed25519 signs over for one audit row.

    The signature covers the SAME fields as the HMAC PLUS the HMAC itself.
    Including the HMAC in the signed payload binds the Ed25519 signature
    to the chain position: an attacker who lifts a valid signature from
    one row cannot replay it onto another row whose HMAC differs (which
    is every other row in the chain).
    """
    payload = _canonical(
        {
            "event_id": str(event_id),
            "session_id": str(session_id),
            "agent_id": agent_id,
            "parent_event_id": str(parent_event_id) if parent_event_id else None,
            "kind": kind,
            "model": model,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "metadata": metadata,
            "prev_hash": prev_hash,
            "created_at": created_at.isoformat(),
            "hmac": hmac_hex,
        }
    )
    return payload.encode("utf-8")


def sign_audit_row(
    row_dict: dict[str, Any],
    signer: Any,
) -> tuple[bytes | None, str | None, str]:
    """Sign an audit row. Returns ``(signature, fingerprint, status)``.

    CONSTRAINT #1 — graceful degradation. If ``signer`` is None this
    returns ``(None, None, "unsigned")``. If the signer raises during
    ``sign()`` (key file unreadable, env var missing, keypair corrupt,
    or any other failure) this function catches the exception, logs
    WARN, and returns ``(None, None, "unsigned_local_failure")``. It
    NEVER raises into host code.

    ``row_dict`` must carry the same keys as the HMAC payload plus
    ``hmac`` (the chain HMAC already computed for this row).

    The ``status`` field is a raw string rather than ``SignatureStatus``
    to avoid an import cycle with ``codeatelier_governance.identity``.
    """
    import structlog

    _log = structlog.get_logger(__name__)

    if signer is None:
        return (None, None, "unsigned")

    try:
        canonical = canonical_row_bytes_for_signing(
            event_id=row_dict["event_id"],
            session_id=row_dict["session_id"],
            agent_id=row_dict["agent_id"],
            parent_event_id=row_dict.get("parent_event_id"),
            kind=row_dict["kind"],
            model=row_dict.get("model"),
            input_hash=row_dict.get("input_hash"),
            output_hash=row_dict.get("output_hash"),
            metadata=row_dict.get("metadata") or {},
            prev_hash=row_dict.get("prev_hash"),
            created_at=row_dict["created_at"],
            hmac_hex=row_dict["hmac"],
        )
        signature = signer.sign(canonical)
        fingerprint = signer.fingerprint
        return (signature, fingerprint, "signed")
    except Exception as exc:  # noqa: BLE001 — degradation is the whole point
        _log.warning(
            "audit.signing_degraded",
            error_type=type(exc).__name__,
            error=str(exc),
            agent_id=row_dict.get("agent_id"),
            detail=(
                "Ed25519 signing failed; row will be written with "
                "signature_status='unsigned_local_failure'. Host call "
                "continues. Check key file permissions, env var presence, "
                "or operator provisioning."
            ),
        )
        return (None, None, "unsigned_local_failure")


def verify_audit_row_signature(
    row_dict: dict[str, Any],
    signature: bytes | None,
    fingerprint: str | None,
    registry: Any,
    revocations: Any = None,
) -> str:
    """Verify an Ed25519 signature on an audit row.

    Returns one of the ``SignatureStatus`` string values:
      * ``"signed"``                 — verified OK.
      * ``"legacy_unsigned"``        — pre-v0.6 row (signature is NULL).
      * ``"unsigned"``               — identity disabled at write time.
      * ``"unsigned_local_failure"`` — preserved from write time.
      * ``"revoked_key"``            — fingerprint revoked at/before chain_seq.
      * ``"unknown_key"``            — fingerprint not in registry.
      * ``"invalid_signature"``      — cryptographic verification failed.

    Does not raise on malformed inputs — every error mode maps to a
    status string so the caller (compliance report, audit explorer) can
    bucket per-row without exception handling.
    """
    if signature is None and fingerprint is None:
        # Pre-v0.6 row OR signing disabled at write time. If the row
        # itself carries a status string, honor it; otherwise default to
        # legacy_unsigned.
        return row_dict.get("signature_status") or "legacy_unsigned"

    if fingerprint is None or signature is None:
        return "invalid_signature"

    if revocations is not None and revocations.is_revoked_at(
        fingerprint, row_dict.get("chain_seq", 0)
    ):
        return "revoked_key"

    record = registry.get_by_fingerprint(fingerprint) if registry is not None else None
    if record is None:
        return "unknown_key"

    # Local import to break a cycle: identity imports from audit.chain in
    # some tests, and audit.chain should not pull identity at module load.
    from ..identity.signer import Ed25519Signer

    canonical = canonical_row_bytes_for_signing(
        event_id=row_dict["event_id"],
        session_id=row_dict["session_id"],
        agent_id=row_dict["agent_id"],
        parent_event_id=row_dict.get("parent_event_id"),
        kind=row_dict["kind"],
        model=row_dict.get("model"),
        input_hash=row_dict.get("input_hash"),
        output_hash=row_dict.get("output_hash"),
        metadata=row_dict.get("metadata") or {},
        prev_hash=row_dict.get("prev_hash"),
        created_at=row_dict["created_at"],
        hmac_hex=row_dict["hmac"],
    )
    ok = Ed25519Signer.verify(canonical, signature, record.public_key_pem)
    return "signed" if ok else "invalid_signature"

# =============================================================================
# === End F6 Track A ==========================================================
# =============================================================================
