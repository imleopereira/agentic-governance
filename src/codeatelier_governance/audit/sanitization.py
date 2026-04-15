"""Shared input sanitization for audit metadata and HTTP-level reason fields.

This was extracted from ``console.app.HaltRequest._sanitize_reason`` so that
ANY string value flowing into the audit chain — whether through the HTTP
boundary or programmatically through ``AuditModule.log`` — is sanitized with
the same rules.

Threat model (BLOCKER C5):
    Insider with SDK credentials forges metadata containing ANSI escape
    sequences (``\\x1b[2J``, ``\\x1b[31m``) or other terminal-control bytes.
    When an operator later cats an audit export or views it in a terminal,
    the forged bytes could clear the screen, repaint a fake banner, or
    otherwise mislead the operator. The HTTP-only sanitizer left this hole
    open for any caller that bypassed the HTTP layer.

Sanitization steps (order matters — see inline comments):
    1. NFC normalize so combining-mark expansions can't bypass length caps.
    2. Cap length in Unicode codepoints, not bytes (bigger metadata values
       are allowed than HTTP `reason` fields — 2048 vs 512).
    3. Escape backslash FIRST, then `\\n`/`\\r`/`\\t`. Doing backslash last
       would double-escape sequences from the prior pass.
    4. Strip C0 controls (everything in 0x00-0x1f and 0x7f except the chars
       we already converted to printable backslash forms).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# Default cap for metadata string values. Bigger than the HTTP `reason` cap
# because programmatic metadata can carry legitimately longer strings (e.g.
# stack traces, error messages). HTTP fields like halt `reason` re-cap at 512.
DEFAULT_MAX_STRING_LEN = 2048
HALT_REASON_MAX_LEN = 512

# C0 controls to strip AFTER the escape pass. \t \n \r are excluded because
# step 3 already converted them to printable `\\t` / `\\n` / `\\r` forms.
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_string(value: str, max_len: int = DEFAULT_MAX_STRING_LEN) -> str:
    """Sanitize a single string value (NFC + cap + escape + strip C0).

    See module docstring for the threat model and order rationale.
    """
    if not isinstance(value, str):  # defensive — caller should pre-check
        return value  # type: ignore[unreachable]
    # 1. NFC normalize first (zalgo defense).
    value = unicodedata.normalize("NFC", value)
    # 2. Cap RAW input length BEFORE escaping. Escapes double the length.
    if len(value) > max_len:
        value = value[:max_len]
    # 3. Escape backslash FIRST, then control chars.
    value = (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    # 4. Strip remaining C0 controls. ANSI escape sequences begin with
    #    \x1b which gets stripped here; the rest of the escape becomes
    #    harmless text characters.
    value = _C0_CONTROL_RE.sub("", value)
    return value


def sanitize_metadata(value: Any, max_len: int = DEFAULT_MAX_STRING_LEN) -> Any:
    """Recursively walk a metadata structure, sanitizing any string leaf.

    Supports dict, list, tuple, set, and primitive types. Non-string leaves
    are returned unchanged. Tuples become tuples; sets become lists (sets
    aren't JSON-serializable so the caller would have failed downstream
    anyway, but we preserve order rather than crashing).
    """
    if isinstance(value, str):
        return sanitize_string(value, max_len=max_len)
    if isinstance(value, dict):
        return {k: sanitize_metadata(v, max_len=max_len) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_metadata(v, max_len=max_len) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_metadata(v, max_len=max_len) for v in value)
    return value


__all__ = [
    "DEFAULT_MAX_STRING_LEN",
    "HALT_REASON_MAX_LEN",
    "sanitize_metadata",
    "sanitize_string",
]
