"""Enforcement-layer allowlist for audit-correction events.

Why this exists
---------------
On 2026-04-14 an operator discovered two days of fake ``console-redesign-*``
audit rows that had been emitted by a misconfigured cron job. Because the
governance audit chain is append-only, the only lawful remediation is to
emit a new ``audit.correction`` event that *references* the offending rows
without deleting them.

Cybersecurity review flagged the correction-write path as a net-new attack
surface: a malicious agent with DB write access could use it to
(a) discredit real evidence, (b) smuggle absolute filesystem paths, git
SHAs, or cron references into the audit surface (leaking infra topology),
or (c) inject XSS into the console render layer.

This module is the single enforcement point for every correction event the
SDK ever writes. No correction may be appended to the audit chain without
passing ``validate_correction_payload`` first.

Sanctioned write path
---------------------
The ONLY sanctioned way to append an ``audit.correction`` event to the
chain is::

    sanitized = validate_correction_payload(raw_payload, hmac_key)
    await sdk.audit.log_validated_correction(sanitized, hmac_key)

Direct calls to ``sdk.audit.log(AuditEvent(kind="audit.correction", ...))``
are refused at the top of ``AuditModule.log`` with a ``RuntimeError``.
This second-layer guard exists because the allowlist below only protects
operators who remember to call it — an adversarial or buggy code path
that constructs the ``AuditEvent`` directly would otherwise bypass every
field / reason / pattern check. The ``log`` guard closes that gap.

Threat model
------------
1. Kind smuggling — attacker sends a payload with ``kind="system.override"``
   instead of ``audit.correction``. Rejected by ``PERMITTED_KINDS``.
2. Field smuggling — attacker appends a field like ``db_pool_size=5`` or
   ``internal_hostname="ops-1"`` hoping it reaches the audit table. Rejected
   by ``ALLOWED_FIELDS``.
3. Path leak — attacker embeds ``/Users/leo/.env`` or ``/srv/gov/cron.sh``
   into ``note``. Rejected by ``FORBIDDEN_NOTE_PATTERNS``.
4. Raw SHA leak — attacker embeds a bare git SHA like ``81b5912``. Rejected
   by ``FORBIDDEN_NOTE_PATTERNS``; operators MUST call ``hash_commit_sha``
   first.
5. IP leak — attacker embeds ``10.0.0.5``. Rejected by pattern.
6. Reason smuggling — attacker writes free-text reasons to escape structured
   query filters. Rejected by ``ALLOWED_REASONS`` enum.
7. XSS — attacker embeds ``<script>alert(1)</script>`` hoping the console
   renders it. Rejected by the same ``<`` / ``>`` forbidden pattern. The
   console is expected to escape at render time anyway, but we defend in
   depth at the write side.
8. Length — attacker floods ``note`` with 1 MB of text to inflate the chain.
   Rejected by ``NOTE_MAX_CHARS``.

Spec
----
Permitted event kinds
    audit.correction   — the only kind allowed through this path

Allowed fields
    kind, ref_chain_seq_start, ref_chain_seq_end, reason, operator_id, note,
    created_at

Allowed reasons (enum)
    operator_marked_erroneous
    agent_theater_retraction
    manual_audit_hygiene

Constraints
    note:       <= NOTE_MAX_CHARS characters, free of forbidden patterns
    operator_id: non-empty string
    ref_chain_seq_start / ref_chain_seq_end: non-negative int, start <= end

Commit-SHA references in an operator note must be replaced with
``hash_commit_sha(sha, hmac_key)`` before submission. The helper returns
a deterministic ``sha256-<12 hex>`` token that preserves provenance without
leaking the raw SHA.
"""
from __future__ import annotations

import hmac
import re
import unicodedata
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

PERMITTED_KINDS: frozenset[str] = frozenset({"audit.correction"})

# Minimum distinct byte count required in an HMAC key. Catches the
# ``b"k" * 32`` / ``b"\x00" * 32`` class of placeholder-secret bugs that
# pass the length check but are trivially guessable. Mirrors the check
# in ``codeatelier_governance.audit.module._check_secret_strength`` but
# inlined here to avoid an import cycle (module → corrections → module).
_MIN_HMAC_KEY_UNIQUE_BYTES = 8


def _check_hmac_key_strength(hmac_key: bytes | bytearray) -> None:
    """Reject HMAC keys that are too short or have trivial entropy.

    Called from ``hash_commit_sha`` and ``validate_correction_payload``
    so every correction-path HMAC consumer inherits the same floor.
    """
    if not isinstance(hmac_key, (bytes, bytearray)) or len(hmac_key) < 16:
        raise CorrectionValidationError(
            field="hmac_key",
            reason="key_too_short",
            operator_hint="Pass the audit HMAC key (>= 16 bytes).",
        )
    if len(set(bytes(hmac_key))) < _MIN_HMAC_KEY_UNIQUE_BYTES:
        raise CorrectionValidationError(
            field="hmac_key",
            reason="key_low_entropy",
            operator_hint=(
                f"HMAC key has < {_MIN_HMAC_KEY_UNIQUE_BYTES} distinct "
                "bytes. This usually means a placeholder like b'k' * 32 "
                "leaked into production. Generate a real key with "
                "secrets.token_bytes(32)."
            ),
        )

NOTE_MAX_CHARS: int = 256

ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "ref_chain_seq_start",
        "ref_chain_seq_end",
        "reason",
        "operator_id",
        "note",
        "created_at",
    }
)

ALLOWED_REASONS: frozenset[str] = frozenset(
    {
        "operator_marked_erroneous",
        "agent_theater_retraction",
        "manual_audit_hygiene",
    }
)

_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"kind", "ref_chain_seq_start", "ref_chain_seq_end", "reason", "operator_id"}
)

# All patterns are case-insensitive and matched against the note field
# AFTER NFKC normalization (which defeats homoglyph / compatibility-form
# evasion). Patterns cover both canonical and adversarially-permuted shapes
# a motivated attacker might use to smuggle infra topology through the
# allowlist. See the threat model in the module docstring.
FORBIDDEN_NOTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Absolute filesystem paths (POSIX)
    re.compile(r"/Users/", re.IGNORECASE),
    re.compile(r"/srv/", re.IGNORECASE),
    re.compile(r"(?:^|[^A-Za-z0-9])/?home/", re.IGNORECASE),
    re.compile(r"/var/", re.IGNORECASE),
    re.compile(r"/tmp/", re.IGNORECASE),
    re.compile(r"/opt/", re.IGNORECASE),
    # URL-encoded path prefixes (%2F = /, %5C = \). An attacker who pipes
    # a percent-encoded path through the correction form would otherwise
    # bypass every literal ``/Users/`` style regex above.
    re.compile(r"%2[Ff](?:Users|srv|home|var|tmp|opt)%2[Ff]", re.IGNORECASE),
    re.compile(r"%5[Cc]", re.IGNORECASE),
    # Windows absolute paths: ``C:\...``, ``D:\...``, UNC ``\\server\share``.
    # The earlier ``.py\b`` / ``.sh\b`` rules caught only a Windows file
    # suffix — a bare ``C:\srv\governance\x`` with no known extension
    # slipped through. Match any drive-letter + backslash prefix or UNC.
    re.compile(r"\b[A-Za-z]:\\"),
    re.compile(r"\\\\[A-Za-z0-9._-]+\\"),
    # Source / script file extensions
    re.compile(r"\.py\b", re.IGNORECASE),
    re.compile(r"\.sh\b", re.IGNORECASE),
    re.compile(r"\.sql\b", re.IGNORECASE),
    # Infra-topology tokens
    re.compile(r"\bcron(?:job)?\b", re.IGNORECASE),
    re.compile(r"\bbranch\b", re.IGNORECASE),
    re.compile(r"console-redesign", re.IGNORECASE),
    # Bare internal hostnames: ``ops-01``, ``db-primary``, ``web-3`` — the
    # naming scheme used across our production inventory. Matches a short
    # role prefix followed by a dash and 1-3 digits, anchored on word
    # boundaries. Legitimate notes never need these tokens; raw hostnames
    # in the audit surface are exactly the infra leak the allowlist blocks.
    re.compile(r"\b(?:ops|db|web|api|gw|lb)-\d{1,3}\b", re.IGNORECASE),
    # Raw git SHAs: 7+ hex chars as a standalone token. The sanctioned
    # ``sha256-<hex>`` hashed token produced by ``hash_commit_sha`` is
    # excluded via a word-boundary-anchored look-behind, case-insensitive
    # so ``SHA256-<hex>`` is ALSO treated as sanctioned (previously the
    # exemption was case-sensitive, meaning an uppercase hashed token
    # would false-reject). The outer ``x`` prefix bypass is still blocked
    # because ``sha256-`` must sit at a word boundary.
    re.compile(
        r"(?<![0-9a-fA-F])"
        r"(?<!(?:^|\b)sha256-)"
        r"(?<!(?:^|\b)SHA256-)"
        r"[0-9a-fA-F]{7,40}"
        r"(?![0-9a-fA-F])"
    ),
    # IPv4 addresses
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # IPv6 addresses — any token with at least one ``::`` or two colon
    # separators and hex groups. Intentionally loose: a false positive on
    # a legitimate note is cheap (rewrite without colons); a false negative
    # leaks network topology. Covers link-local (``fe80::1``), doc range
    # (``2001:db8::5``), and compressed forms.
    re.compile(
        r"\b(?:[0-9a-fA-F]{1,4}:){3,}[0-9a-fA-F]{0,4}\b"
    ),
    re.compile(r"::[0-9a-fA-F]{1,4}"),
    # CRLF injection: log-line splitting into auxiliary fields is a known
    # audit-forwarder exploit. No legitimate correction note spans lines.
    re.compile(r"[\r\n]"),
    # Null byte: string-termination bugs downstream + C-layer splitting.
    re.compile(r"\x00"),
    # HTML tags (XSS defense-in-depth)
    re.compile(r"<[^>]+>"),
)


class CorrectionValidationError(ValueError):
    """Raised when a correction payload fails the allowlist check.

    Attributes:
        field: the offending field name (``kind``, ``note``, ``reason``,
            ``operator_id``, ``ref_chain_seq_start``, ``ref_chain_seq_end``,
            or the literal name of an unknown field).
        reason: a machine-readable reason slug.
        operator_hint: a one-line fix hint for the human operator.
    """

    def __init__(self, field: str, reason: str, operator_hint: str) -> None:
        self.field = field
        self.reason = reason
        self.operator_hint = operator_hint
        super().__init__(f"{field}: {reason} ({operator_hint})")


def hash_commit_sha(sha: str, hmac_key: bytes) -> str:
    """Return a log-safe hashed token for a git commit SHA.

    The output is ``sha256-<first 12 hex chars of HMAC-SHA256(key, sha)>``.
    Deterministic for a given ``(sha, hmac_key)`` pair, so operators can
    still cross-reference corrections to the original commit, but the raw
    SHA never lands in the audit table.
    """
    if not isinstance(sha, str) or not sha:
        raise CorrectionValidationError(
            field="sha",
            reason="empty_or_non_string",
            operator_hint="Pass the full commit SHA as a non-empty string.",
        )
    _check_hmac_key_strength(hmac_key)
    digest = hmac.new(bytes(hmac_key), sha.encode("utf-8"), sha256).hexdigest()
    return f"sha256-{digest[:12]}"


def _validate_kind(payload: dict[str, Any]) -> str:
    kind = payload.get("kind")
    if not isinstance(kind, str):
        raise CorrectionValidationError(
            field="kind",
            reason="missing_or_non_string",
            operator_hint="Set kind='audit.correction'.",
        )
    if kind not in PERMITTED_KINDS:
        raise CorrectionValidationError(
            field="kind",
            reason=f"kind_{kind!r}_not_in_allowlist",
            operator_hint="Only 'audit.correction' is permitted on this path.",
        )
    return kind


def _validate_reason(payload: dict[str, Any]) -> str:
    reason = payload.get("reason")
    if not isinstance(reason, str):
        raise CorrectionValidationError(
            field="reason",
            reason="missing_or_non_string",
            operator_hint=(
                "Pick one of: " + ", ".join(sorted(ALLOWED_REASONS))
            ),
        )
    if reason not in ALLOWED_REASONS:
        raise CorrectionValidationError(
            field="reason",
            reason="free_text_reason_rejected",
            operator_hint=(
                "Free-text reasons are blocked. Use one of: "
                + ", ".join(sorted(ALLOWED_REASONS))
            ),
        )
    return reason


def _validate_operator_id(payload: dict[str, Any]) -> str:
    op = payload.get("operator_id")
    if not isinstance(op, str) or not op.strip():
        raise CorrectionValidationError(
            field="operator_id",
            reason="missing_or_empty",
            operator_hint="Pass a non-empty operator_id (UUID or user login).",
        )
    return op


def _validate_seq_bounds(payload: dict[str, Any]) -> tuple[int, int]:
    start = payload.get("ref_chain_seq_start")
    end = payload.get("ref_chain_seq_end")
    for name, value in (
        ("ref_chain_seq_start", start),
        ("ref_chain_seq_end", end),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise CorrectionValidationError(
                field=name,
                reason="missing_or_non_int",
                operator_hint=f"{name} must be a non-negative int.",
            )
        if value < 0:
            raise CorrectionValidationError(
                field=name,
                reason="negative",
                operator_hint=f"{name} must be >= 0.",
            )
    # Mypy narrowing: both are ints at this point.
    assert isinstance(start, int)
    assert isinstance(end, int)
    if start > end:
        raise CorrectionValidationError(
            field="ref_chain_seq_end",
            reason="end_before_start",
            operator_hint="ref_chain_seq_end must be >= ref_chain_seq_start.",
        )
    return start, end


def _validate_note(payload: dict[str, Any]) -> str:
    note = payload.get("note", "")
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise CorrectionValidationError(
            field="note",
            reason="non_string",
            operator_hint="note must be a string (max 256 chars).",
        )
    if len(note) > NOTE_MAX_CHARS:
        raise CorrectionValidationError(
            field="note",
            reason=f"over_{NOTE_MAX_CHARS}_chars",
            operator_hint=f"Truncate note to <= {NOTE_MAX_CHARS} characters.",
        )
    # NFKC-normalize before scanning so compatibility-form homoglyph
    # evasion (e.g. a full-width ``/`` U+FF0F that renders identical to
    # ASCII ``/``) collapses to the canonical form the patterns expect.
    # The scanned string is a local copy; the stored note is the original
    # operator input so downstream rendering is unchanged.
    scan_target = unicodedata.normalize("NFKC", note)
    for pattern in FORBIDDEN_NOTE_PATTERNS:
        if pattern.search(scan_target):
            raise CorrectionValidationError(
                field="note",
                reason=f"forbidden_pattern:{pattern.pattern}",
                operator_hint=(
                    "Remove filesystem paths (POSIX, Windows, URL-encoded), "
                    "source file names, cron/branch references, raw git SHAs, "
                    "IP addresses (v4/v6), internal hostnames, CRLF or null "
                    "bytes, and HTML tags. Use hash_commit_sha() for commit "
                    "references."
                ),
            )
    return note


def _validate_created_at(value: Any) -> datetime:
    """Validate the optional ``created_at`` field.

    Accepts only:
        * timezone-aware ``datetime`` (naive datetimes are rejected so a
          stray localtime never leaks onto the audit surface)
        * ISO-8601 string parseable by ``datetime.fromisoformat`` — a
          trailing ``Z`` is normalized to ``+00:00`` so standard UTC
          representations round-trip cleanly.

    Anything else (``int``, ``dict``, ``None``, SQL-ish string, etc.)
    is rejected with a ``CorrectionValidationError``. This closes the
    raw-passthrough hole where an attacker could set ``created_at`` to
    an arbitrary object and have it carry through to the audit table
    without any type check.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise CorrectionValidationError(
                field="created_at",
                reason="naive_datetime",
                operator_hint=(
                    "created_at must be timezone-aware. Use "
                    "datetime.now(timezone.utc) or an ISO-8601 UTC string."
                ),
            )
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise CorrectionValidationError(
                field="created_at",
                reason="invalid_iso8601",
                operator_hint=(
                    "created_at must be an ISO-8601 UTC string "
                    "(e.g. '2026-04-15T12:00:00Z')."
                ),
            ) from exc
        if parsed.tzinfo is None:
            raise CorrectionValidationError(
                field="created_at",
                reason="naive_datetime",
                operator_hint=(
                    "created_at string must include a timezone offset "
                    "(e.g. trailing 'Z' or '+00:00')."
                ),
            )
        return parsed.astimezone(timezone.utc)
    raise CorrectionValidationError(
        field="created_at",
        reason="invalid_type",
        operator_hint=(
            "created_at must be a timezone-aware datetime or an ISO-8601 "
            "UTC string; got type " + type(value).__name__ + "."
        ),
    )


def validate_correction_payload(
    payload: dict[str, Any],
    hmac_key: bytes,
) -> dict[str, Any]:
    """Validate an audit-correction payload against the v0.6 allowlist.

    Parameters
    ----------
    payload:
        The raw correction dict as produced by the operator CLI / console.
    hmac_key:
        The governance audit HMAC key. Accepted for symmetry with
        ``hash_commit_sha`` — the allowlist itself does not hash anything,
        but consumers typically pass the key through.

    Returns
    -------
    dict[str, Any]
        A new dict containing exactly the validated fields, safe to append
        to the audit chain.

    Raises
    ------
    CorrectionValidationError
        On any violation. The ``field`` attribute names the failed field.
    """
    if not isinstance(payload, dict):
        raise CorrectionValidationError(
            field="payload",
            reason="not_a_dict",
            operator_hint="payload must be a dict.",
        )
    if not payload:
        raise CorrectionValidationError(
            field="payload",
            reason="empty",
            operator_hint="payload must include kind, reason, operator_id, "
            "ref_chain_seq_start, ref_chain_seq_end.",
        )
    _check_hmac_key_strength(hmac_key)

    # Unknown-field rejection first: fail before we touch any value.
    for key in payload.keys():
        if key not in ALLOWED_FIELDS:
            raise CorrectionValidationError(
                field=key,
                reason="field_not_in_allowlist",
                operator_hint=(
                    "Remove this field. Allowed: " + ", ".join(sorted(ALLOWED_FIELDS))
                ),
            )

    # Validate note FIRST so forbidden content (paths, SHAs, IPs, HTML) is
    # rejected even when other required fields are also missing. Security-
    # critical: we want the most dangerous class of error to surface first.
    note = _validate_note(payload)

    # Required fields presence
    missing = _REQUIRED_FIELDS - payload.keys()
    if missing:
        field_name = sorted(missing)[0]
        raise CorrectionValidationError(
            field=field_name,
            reason="required_field_missing",
            operator_hint=f"Add {field_name} to the payload.",
        )

    kind = _validate_kind(payload)
    reason = _validate_reason(payload)
    operator_id = _validate_operator_id(payload)
    seq_start, seq_end = _validate_seq_bounds(payload)

    sanitized: dict[str, Any] = {
        "kind": kind,
        "reason": reason,
        "operator_id": operator_id,
        "ref_chain_seq_start": seq_start,
        "ref_chain_seq_end": seq_end,
        "note": note,
    }
    if "created_at" in payload:
        sanitized["created_at"] = _validate_created_at(payload["created_at"])
    return sanitized


__all__ = [
    "ALLOWED_FIELDS",
    "ALLOWED_REASONS",
    "CorrectionValidationError",
    "FORBIDDEN_NOTE_PATTERNS",
    "NOTE_MAX_CHARS",
    "PERMITTED_KINDS",
    "hash_commit_sha",
    "validate_correction_payload",
]
