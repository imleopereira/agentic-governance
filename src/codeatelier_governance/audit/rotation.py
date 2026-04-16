"""F6 Track B — chain key rotation procedure.

Writes a dual-signed rotation marker row, registers the incoming key
version in ``governance_audit_chain_keys``, and retires the outgoing key
version. The entire flow is a single transaction: partial state is never
observable to a verifier.

The host application continues to work if this rotation fails (design
invariant 1). The rotate-chain-key CLI command exits non-zero and
operators retry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .chain import (
    KEY_ROTATION_KIND,
    compute_rotation_marker_macs,
)
from .keys import fingerprint_key

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RotationResult:
    """Returned by :func:`rotate_chain_key` on success."""

    outgoing_fingerprint: str
    incoming_fingerprint: str
    marker_event_id: UUID
    marker_chain_seq: int
    outgoing_key_version: int
    incoming_key_version: int


async def rotate_chain_key(
    engine: AsyncEngine,
    *,
    outgoing_secret: bytes,
    incoming_secret: bytes,
    operator_id: str,
    rotation_reason: str = "scheduled",
    session_id: UUID | None = None,
    agent_id: str = "governance.system",
) -> RotationResult:
    """Atomically rotate the HMAC chain key.

    Steps (all in one transaction):
        1. Compute fingerprints for outgoing + incoming keys.
        2. Look up the outgoing key version by fingerprint.
        3. Determine the next chain_seq for the marker row.
        4. Build the dual-signed marker payload.
        5. INSERT the marker into governance_audit_events.
        6. INSERT the new row in governance_audit_chain_keys with
           activated_at_chain_seq = marker.chain_seq + 1.
        7. UPDATE the outgoing row's retired_at_chain_seq = marker.chain_seq.

    The outgoing and incoming key material must be held by the caller;
    never serialize it through this function's return value or logs.
    """
    if outgoing_secret == incoming_secret:
        raise ValueError(
            "rotate_chain_key: incoming key is identical to outgoing key. "
            "Nothing to rotate. Fix: generate a new key."
        )
    outgoing_fp = fingerprint_key(outgoing_secret)
    incoming_fp = fingerprint_key(incoming_secret)

    # Default session ID if caller didn't supply one: deterministic system
    # namespace so all governance internal events cluster together.
    sid = session_id or UUID("00000000-0000-0000-0000-000000000001")
    event_id = uuid4()
    created_at = datetime.now(timezone.utc)

    async with engine.begin() as conn:
        # 1. Find the outgoing key version
        res = await conn.execute(
            text(
                "SELECT key_version FROM governance_audit_chain_keys "
                "WHERE fingerprint = :fp AND retired_at_chain_seq IS NULL "
                "ORDER BY key_version DESC LIMIT 1"
            ),
            {"fp": outgoing_fp},
        )
        row = res.first()
        if row is None:
            raise RuntimeError(
                f"rotate_chain_key: outgoing fingerprint "
                f"{outgoing_fp[:16]}... is not registered as an active key "
                f"version. Bootstrap it first."
            )
        outgoing_version = int(row[0])

        # 2. Reserve the next chain_seq for the marker row. We insert the
        # marker FIRST with placeholder MAC fields, read back the assigned
        # chain_seq, and then... actually, the MACs depend on prev_hash,
        # which depends on the last event already in the chain. Compute
        # everything in application space up front.
        res = await conn.execute(
            text(
                "SELECT chain_seq, hmac_value FROM governance_audit_events "
                "ORDER BY chain_seq DESC LIMIT 1"
            )
        )
        prev_row = res.first()
        prev_hash = prev_row[1] if prev_row is not None else None

        metadata: dict[str, Any] = {
            "outgoing_fingerprint": outgoing_fp,
            "incoming_fingerprint": incoming_fp,
            "outgoing_key_version": outgoing_version,
            "rotation_reason": rotation_reason,
            "operator_id": operator_id,
        }

        outgoing_mac, incoming_mac = compute_rotation_marker_macs(
            outgoing_secret=outgoing_secret,
            incoming_secret=incoming_secret,
            event_id=event_id,
            session_id=sid,
            agent_id=agent_id,
            parent_event_id=None,
            metadata=metadata,
            prev_hash=prev_hash,
            created_at=created_at,
        )

        # 3. INSERT the marker row.
        # NOTE: hmac_next column lives in the F6 Track B migration; it is
        # nullable on non-marker rows and populated here.
        import json as _json

        res = await conn.execute(
            text(
                "INSERT INTO governance_audit_events "
                "(event_id, session_id, agent_id, parent_event_id, kind, "
                " input_hash, output_hash, metadata_json, prev_hash, "
                " hmac_value, hmac_next, created_at) "
                "VALUES (:eid, :sid, :aid, NULL, :kind, NULL, NULL, "
                " CAST(:meta AS JSONB), :prev, :hmac_out, :hmac_in, :ts) "
                "RETURNING chain_seq"
            ),
            {
                "eid": str(event_id),
                "sid": str(sid),
                "aid": agent_id,
                "kind": KEY_ROTATION_KIND,
                "meta": _json.dumps(metadata),
                "prev": prev_hash,
                "hmac_out": outgoing_mac,
                "hmac_in": incoming_mac,
                "ts": created_at,
            },
        )
        marker_seq = int(res.scalar_one())

        # 4. Register incoming key version starting at marker_seq + 1.
        res = await conn.execute(
            text(
                "INSERT INTO governance_audit_chain_keys "
                "(fingerprint, activated_at_chain_seq) "
                "VALUES (:fp, :seq) "
                "RETURNING key_version"
            ),
            {"fp": incoming_fp, "seq": marker_seq + 1},
        )
        incoming_version = int(res.scalar_one())

        # 5. Retire outgoing. Semantic: retired_at_chain_seq is the first
        # chain_seq that the outgoing key does NOT authenticate. Since the
        # marker row IS part of the old segment (off-by-one rule, design
        # doc section 6), the outgoing key must still cover marker_seq.
        # Therefore we retire at marker_seq + 1 — which equals the incoming
        # key's activated_at_chain_seq, producing a seamless handoff.
        await conn.execute(
            text(
                "UPDATE governance_audit_chain_keys "
                "SET retired_at_chain_seq = :seq "
                "WHERE key_version = :kv AND retired_at_chain_seq IS NULL"
            ),
            {"seq": marker_seq + 1, "kv": outgoing_version},
        )

    _logger.info(
        "audit.chain_key_rotated",
        outgoing_fingerprint_prefix=outgoing_fp[:16] + "...",
        incoming_fingerprint_prefix=incoming_fp[:16] + "...",
        marker_chain_seq=marker_seq,
        rotation_reason=rotation_reason,
    )

    return RotationResult(
        outgoing_fingerprint=outgoing_fp,
        incoming_fingerprint=incoming_fp,
        marker_event_id=event_id,
        marker_chain_seq=marker_seq,
        outgoing_key_version=outgoing_version,
        incoming_key_version=incoming_version,
    )
