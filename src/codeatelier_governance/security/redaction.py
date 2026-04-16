"""Secret-pattern redaction for any JSON-serializable value.

Strips well-known secret-shaped substrings from any JSON-serializable value
before it is handed to an outbound surface (console response, OTel span
attribute, log line, ...). Complements — but does NOT replace — input-side
sanitization (NFC normalisation, ANSI stripping, size caps): those are shape
controls, this is a content control.

Threat model: a stray user-controlled string (policy note, gate rationale,
agent metadata, stack trace, error message) contains a secret that
accidentally lands in an outbound payload. A shape-level model would happily
serialize it because the field is declared as a string. This module masks
the value before the outbound surface sees it.

Patterns covered (F3 PRD + Cybersec mandate):

* ``sk-ant-...``                 Anthropic API key
* ``sk-...``                     OpenAI API key (also covers other ``sk-`` families)
* ``xoxb-...``                   Slack bot token
* ``gh[ps]_...``                 GitHub personal access token (classic + fine-grained)
* ``AKIA[A-Z0-9]{16}``           AWS access key ID
* ``aws_secret_access_key=...``  AWS secret access key (keyed form)
* ``postgres[ql]://`` / ``mysql://`` / ``mongodb://`` / ``redis[s]://``
  database DSNs with or without embedded ``user:pass@`` credentials — a
  stack trace leaking a DSN into audit metadata is a common accident,
  and any embedded ``user:pass`` is the highest-value secret in the line.

Stdlib-only per CLAUDE.md: no new dependencies.

Layering: this module lives under ``security/`` (not ``console/`` or
``audit/``) because both packages call it and neither depends on the other.
Prior to v0.6.1 it lived at ``codeatelier_governance.console.redaction``;
that path remains importable as an identity alias until v0.7.
"""
from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"

# Order matters only in that the first pattern to match a given span wins for
# that span. All patterns are applied in sequence to every string, so more
# specific prefixes (``sk-ant-``) must come before the broader ones (``sk-``)
# to avoid double-substitution of an already-redacted span.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Database DSNs FIRST — these commonly carry embedded user:pass
    # credentials and we want to redact the entire connection string rather
    # than let a later pattern nibble at a substring. Matches non-whitespace
    # after the scheme to greedily consume the whole DSN up to the next
    # whitespace char. Mirrors the frontend ``sanitizeErrorMessage`` DSN
    # pass in ``console/src/lib/connectionStore.ts``.
    (
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss):"
            r"//\S+",
            re.IGNORECASE,
        ),
        _REDACTED,
    ),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), _REDACTED),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), _REDACTED),
    (re.compile(r"xoxb-[A-Za-z0-9\-]{20,}"), _REDACTED),
    (re.compile(r"gh[ps]_[A-Za-z0-9]{30,}"), _REDACTED),
    (re.compile(r"AKIA[A-Z0-9]{16}"), _REDACTED),
    (
        re.compile(
            r"(?i)aws_secret_access_key[\"\s:=]+[A-Za-z0-9/+=]{40}"
        ),
        _REDACTED,
    ),
)


def redact_secrets(value: Any) -> Any:
    """Recursively redact known secret patterns from a JSON-serializable value.

    Walks ``dict``/``list``/``tuple`` structures. Non-string leaves
    (int/float/bool/None) pass through unchanged. Returns a NEW structure —
    does not mutate the input, so caller-owned dicts from the ORM layer
    stay clean.
    """
    if isinstance(value, str):
        out = value
        for pattern, replacement in _SECRET_PATTERNS:
            out = pattern.sub(replacement, out)
        return out
    if isinstance(value, dict):
        return {k: redact_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    return value


__all__ = ["redact_secrets"]
