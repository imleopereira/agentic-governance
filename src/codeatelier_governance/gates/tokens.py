"""Signed approval token construction and verification.

Token format:
    f"{request_id}:{action_hash}:{expires_at_iso}:{hmac_hex}"

The HMAC is computed over the first three fields with the gates secret. A
forged or replayed token fails verification because:
    * Forged: attacker can't produce a valid hmac without the secret.
    * Replayed: tokens are single-use; the server-side state set rejects
      duplicates (enforced in module.py, not here).
    * Tampered (e.g., changed action_hash): the recomputed hmac mismatches.
    * Expired: expires_at is checked against the current UTC time.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from uuid import UUID

from .errors import ApprovalTokenError


def make_token(
    *,
    secret: bytes,
    request_id: UUID,
    action_hash: str,
    expires_at: datetime,
) -> str:
    """Build a signed approval token.

    The hmac binds request_id, action_hash, and expires_at together so that
    no piece can be swapped out without breaking the signature.
    """
    body = f"{request_id}:{action_hash}:{expires_at.isoformat()}"
    mac = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}:{mac}"


def parse_token(
    *,
    secret: bytes,
    token: str,
) -> tuple[UUID, str, datetime]:
    """Verify and parse a signed approval token.

    Returns ``(request_id, action_hash, expires_at)`` on success. Raises
    :class:`ApprovalTokenError` on any failure.
    """
    parts = token.split(":")
    if len(parts) < 4:
        raise ApprovalTokenError("approval token: malformed")
    # The hmac is the LAST segment; everything before it is the body. The
    # ISO timestamp contains colons, so naive splits would lose data.
    body = ":".join(parts[:-1])
    provided_mac = parts[-1]
    expected_mac = hmac.new(
        secret, body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_mac, provided_mac):
        raise ApprovalTokenError("approval token: signature mismatch")

    body_parts = body.split(":")
    if len(body_parts) < 3:
        raise ApprovalTokenError("approval token: malformed body")
    request_id_s = body_parts[0]
    action_hash = body_parts[1]
    expires_iso = ":".join(body_parts[2:])
    try:
        request_id = UUID(request_id_s)
    except ValueError as exc:
        raise ApprovalTokenError("approval token: bad request_id") from exc
    try:
        expires_at = datetime.fromisoformat(expires_iso)
    except ValueError as exc:
        raise ApprovalTokenError("approval token: bad expiration") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise ApprovalTokenError("approval token: expired")
    return request_id, action_hash, expires_at
