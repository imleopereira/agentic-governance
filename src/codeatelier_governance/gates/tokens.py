"""Signed approval token construction and verification.

v2 Token format (v0.6.2, rotation-aware):
    f"v2:{key_prefix}:{request_id}:{action_hash}:{expires_at_iso}:{hmac_hex}"

Legacy v1 token format (pre-v0.6.2, single-key):
    f"{request_id}:{action_hash}:{expires_at_iso}:{hmac_hex}"

The HMAC is computed over the body (everything up to, but not including,
the final ``hmac_hex`` segment) with the gates secret. A forged or replayed
token fails verification because:
    * Forged: attacker can't produce a valid hmac without the secret.
    * Replayed: tokens are single-use; the server-side state set rejects
      duplicates (enforced in module.py, not here).
    * Tampered (e.g., changed action_hash): the recomputed hmac mismatches.
    * Expired: expires_at is checked against the current UTC time.

v0.6.2 P0 — rotation awareness. Pre-v0.6.2 tokens embedded no key identity.
After an operator rotated the gates/audit secret (which share the same env
var in practice), every lawfully-minted pre-rotation token failed the
current-secret HMAC check and was rejected with ``signature mismatch``.
That is a silent data-loss footgun on the human-approval critical path.

Fix: v2 tokens embed the first 16 chars of the salted fingerprint of the
signing key. On parse, the verifier resolves the historical key through
the same ``GOVERNANCE_CHAIN_KEY_<prefix>`` env-var protocol used by the
audit chain verifier (see ``audit.keys.resolve_key``), then verifies the
HMAC under that resolved key. When the historical key material is not
mounted on the verifying process, we raise a DISTINCT ``historical key
unavailable`` error so operators can distinguish a missing env var from
a tampered / forged token.

Tokens minted before v0.6.2 (no ``v2:`` prefix) continue to verify under
the current ``secret`` argument only — we can't retroactively bind them
to a key fingerprint, and changing the legacy format would break every
in-flight approval token at the moment of upgrade.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import warnings
from datetime import datetime, timezone
from uuid import UUID

import structlog

# Reach-through to the audit-chain URI resolver. Gates and audit share
# the same env-var protocol (GOVERNANCE_CHAIN_KEY_<prefix>=env://VAR or
# file:///abs/path) for historical key material — by dogfooding the same
# resolver, operators who already set up the env vars for the audit chain
# get gate-token rotation-awareness for free.
from ..audit.keys import _resolve_uri, fingerprint_key
from .errors import ApprovalTokenError, TokenVersionTooOldError

logger = structlog.get_logger(__name__)

# Token format version tag. Embedded as the FIRST colon-separated segment
# of every v0.6.2+ token. The parser detects the ``v2:`` sentinel to pick
# the rotation-aware verification path; tokens lacking the sentinel go
# down the legacy single-key path (pre-v0.6.2 minted tokens).
TOKEN_VERSION_V2 = "v2"

# Number of hex chars of the fingerprint we embed as the token's key tag.
# Matches the audit chain env-var protocol (GOVERNANCE_CHAIN_KEY_<first16>)
# so operators can provision historical gate-key material with the same
# env var they already use for the audit chain.
KEY_PREFIX_LEN = 16


def _key_prefix(secret: bytes) -> str:
    """Return the 16-char fingerprint prefix used as the token's key tag."""
    return fingerprint_key(secret)[:KEY_PREFIX_LEN]


def _resolve_secret_for_prefix(
    prefix: str, current_secret: bytes
) -> bytes | None:
    """Resolve a key prefix back to raw secret bytes.

    Returns:
        * ``current_secret`` when ``prefix`` matches the current secret's
          fingerprint prefix (fast path — no env lookup).
        * raw bytes of the historical key when the operator has provisioned
          ``GOVERNANCE_CHAIN_KEY_<prefix>`` (env:// or file:// URI, same
          protocol as the audit chain verifier).
        * ``None`` when the prefix is unknown — caller surfaces this as
          the distinct ``historical key unavailable`` error so operators
          can distinguish "forgot to mount the rotated-away key" from
          a tampered signature.
    """
    if _key_prefix(current_secret) == prefix:
        return current_secret
    env_name = f"GOVERNANCE_CHAIN_KEY_{prefix}"
    uri = os.environ.get(env_name)
    if not uri:
        return None
    # Same URI decoder the audit chain uses — env:// (base64 or raw UTF-8)
    # or file:///path. Returning None here falls into the "historical key
    # unavailable" branch, which is the correct answer when the URI
    # failed to resolve (file missing, env var empty, etc.).
    material = _resolve_uri(uri)
    if material is None:
        return None
    # Defense in depth: the env var must point at a key whose fingerprint
    # actually matches the embedded prefix. If the operator wired
    # GOVERNANCE_CHAIN_KEY_<prefix> to the wrong key, we refuse to verify
    # under it — "unavailable" is the right answer, not "resolved to
    # wrong bytes that happen to hash to something". Mirrors the same
    # defense in ``audit.keys._cached_resolve``.
    if _key_prefix(material) != prefix:
        return None
    return material


def make_token(
    *,
    secret: bytes,
    request_id: UUID,
    action_hash: str,
    expires_at: datetime,
    use_v2: bool = True,
) -> str:
    """Build a signed approval token.

    When ``use_v2=True`` (default) the token includes a ``v2:<key_prefix>:``
    prefix binding the token to the key version active at issuance. See
    the module docstring for the rotation-aware verification story.

    When ``use_v2=False`` the token uses the pre-v0.6.2 legacy format
    (no version tag, no key prefix). This is the DOWNGRADE-SAFE mode
    used during rolling deploys of mixed v0.6.1 + v0.6.2 pods: a v0.6.1
    pod handling the grant/deny cannot parse ``v2:`` tokens and would
    reject them as ``bad request_id``. Operators who know every pod is
    on v0.6.2+ should pass ``use_v2=True`` (or set
    ``GatesModule(enable_v2_tokens=True)``) to activate rotation-awareness
    for newly-minted tokens. Flip becomes the default in v0.6.3.

    The hmac binds every token field (version tag or its absence, key
    prefix, request_id, action_hash, expires_at) so no piece can be
    swapped out without breaking the signature.
    """
    if use_v2:
        prefix = _key_prefix(secret)
        body = (
            f"{TOKEN_VERSION_V2}:{prefix}:{request_id}:"
            f"{action_hash}:{expires_at.isoformat()}"
        )
    else:
        # Legacy v1 body — identical to pre-v0.6.2. Allows a v0.6.1 pod
        # in a rolling-deploy window to parse and verify tokens minted
        # by the adjacent v0.6.2 pod under the same secret.
        body = f"{request_id}:{action_hash}:{expires_at.isoformat()}"
    mac = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}:{mac}"


def parse_token(
    *,
    secret: bytes,
    token: str,
    accept_v1_until: datetime | None = None,
) -> tuple[UUID, str, datetime]:
    """Verify and parse a signed approval token.

    Returns ``(request_id, action_hash, expires_at)`` on success.

    Parameters
    ----------
    secret:
        Current gates secret. v2 tokens may verify under a rotated-away
        key resolved via ``GOVERNANCE_CHAIN_KEY_<prefix>``; v1 legacy
        tokens always verify under this ``secret`` directly.
    token:
        The signed approval token to verify.
    accept_v1_until:
        Cutoff date past which legacy v1 (pre-v0.6.2) tokens are
        rejected with :class:`TokenVersionTooOldError`. ``None`` means
        no sunset (accept indefinitely — only used by callers who have
        opted out of the sunset, e.g. tests of legacy back-compat).
        ``GatesModule`` passes a datetime ~90 days after v0.6.2 release
        by default. Operators can override via
        ``GOVERNANCE_GATES_ACCEPT_V1_UNTIL`` (honored by ``GatesModule``,
        not by this function directly).

    Raises :class:`ApprovalTokenError` on any failure. The error message
    distinguishes:
        * ``"historical key unavailable"`` — the token was minted under a
          key that has since been rotated away AND the operator has NOT
          provisioned ``GOVERNANCE_CHAIN_KEY_<prefix>``. Distinct from
          ``signature mismatch`` because the remediation is different
          (mount the old key, don't treat as forgery).
        * ``"signature mismatch"`` — HMAC verification failed under the
          resolved key. Genuine tampering / forgery indicator.
        * ``"expired"`` — ``expires_at`` has passed.
        * ``"malformed"`` / ``"bad ..."`` — structural parse failure.
        * :class:`TokenVersionTooOldError` — legacy v1 format past the
          configured ``accept_v1_until`` sunset. Distinct subclass so
          operator UIs can show a specific "deployment aged out v1
          tokens" message and suggest the grace-window override.
    """
    parts = token.split(":")
    if len(parts) < 4:
        raise ApprovalTokenError("approval token: malformed")
    # The hmac is the LAST segment; everything before it is the body. The
    # ISO timestamp contains colons, so naive splits would lose data.
    body = ":".join(parts[:-1])
    provided_mac = parts[-1]
    body_parts = body.split(":")

    # Detect v2 format. Body starts with ``v2:<prefix>:`` where prefix is
    # exactly 16 hex chars. Absence of the sentinel falls through to the
    # legacy single-key verification path so in-flight pre-v0.6.2 tokens
    # do not break at the moment of upgrade.
    verify_secret: bytes = secret
    if len(body_parts) >= 2 and body_parts[0] == TOKEN_VERSION_V2:
        prefix = body_parts[1]
        if len(prefix) != KEY_PREFIX_LEN:
            raise ApprovalTokenError(
                "approval token: malformed key prefix"
            )
        resolved = _resolve_secret_for_prefix(prefix, secret)
        if resolved is None:
            # Distinct error code so operators can tell "rotated-away key
            # not mounted" apart from "signature is forged". An attacker
            # can of course fabricate a token with an unknown prefix to
            # pin this branch — but the remediation is identical at the
            # UX layer (reject the token) so no information leak matters.
            raise ApprovalTokenError(
                "approval token: historical key unavailable"
            )
        verify_secret = resolved
        rid_idx = 2
        action_hash_idx = 3
        exp_start_idx = 4
    else:
        # Legacy v1 path: pre-v0.6.2 minted tokens, no key prefix. Verify
        # under the current secret only. If the operator has since
        # rotated, these tokens WILL fail — but there's no way to know
        # which historical key to use without the embedded prefix, and
        # legacy tokens expire within an hour by default, so the blast
        # radius is a one-hour migration window at first rotation.
        #
        # v0.6.2-followup UPGRADE-BREAKER: a v1-shape token is parseable
        # forever in principle. An attacker who ever learned the current
        # secret can mint v1 forgeries bypassing v2 rotation-awareness.
        # ``accept_v1_until`` closes this by rejecting v1 past a cutoff
        # (default ~90d post v0.6.2 ship, via GatesModule). The
        # DeprecationWarning fires on every successful legacy parse so
        # customers see the deadline coming in their test suites (pytest
        # -W error::DeprecationWarning) and structured logs.
        now = datetime.now(timezone.utc)
        if accept_v1_until is not None and now > accept_v1_until:
            raise TokenVersionTooOldError(
                f"approval token: legacy v1 format rejected — this "
                f"deployment stopped accepting pre-v0.6.2 tokens on "
                f"{accept_v1_until.isoformat()}. Set "
                f"GOVERNANCE_GATES_ACCEPT_V1_UNTIL=<ISO date> for a "
                f"grace-window override (logged as "
                f"gates.v1_sunset_overridden). Re-mint the approval "
                f"request under v0.6.2+ to get a v2 token."
            )
        warnings.warn(
            "Parsing a legacy v1 (pre-v0.6.2) gates approval token. "
            "v1 tokens do not embed a key-fingerprint prefix and cannot "
            "verify under a rotated-away key. Re-mint under v0.6.2+ to "
            "get v2 rotation-awareness.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.info(
            "gates.legacy_token_parsed",
            detail=(
                "v1 (pre-v0.6.2) approval token verified under the "
                "current secret. Legacy path is scheduled for removal; "
                "see GatesModule.accept_v1_until."
            ),
        )
        rid_idx = 0
        action_hash_idx = 1
        exp_start_idx = 2

    expected_mac = hmac.new(
        verify_secret, body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_mac, provided_mac):
        raise ApprovalTokenError("approval token: signature mismatch")

    if len(body_parts) < exp_start_idx + 1:
        raise ApprovalTokenError("approval token: malformed body")
    request_id_s = body_parts[rid_idx]
    action_hash = body_parts[action_hash_idx]
    expires_iso = ":".join(body_parts[exp_start_idx:])
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
