"""Formula-injection-safe CSV export helpers for cost data.

Spreadsheet formula injection (OWASP CSV Injection, CVE-2014-3524 family)
happens when a CSV cell beginning with ``= + - @`` — or tab/CR that a parser
re-interprets as leading whitespace before one of those — is auto-evaluated
on open by Excel, Google Sheets, LibreOffice, Numbers. A malicious
``agent_id = "=HYPERLINK(\"evil.com\", \"click me\")"`` lands as a live
hyperlink in the compliance officer's spreadsheet.

Our defense: prefix-quote (``"'" + cell``) any string cell beginning with
a dangerous character. Google Sheets' own guidance plus OWASP both
recommend this over stripping — we never lose data, just make the cell
inert as a formula.

This module ONLY exposes ``_csv_safe`` and ``write_rows``. It intentionally
has no model imports so it stays import-cheap (DX: /cost CSV export should
not drag in SQLAlchemy).
"""
from __future__ import annotations

import csv
import io
from typing import Any, Iterable, Sequence

# Per OWASP + Excel docs, the dangerous set is {=, +, -, @, \t, \r}. Spaces
# at the front of a real agent_id are not expected but we treat a leading
# tab/CR as "could be reparsed to trigger formula eval in some consumers".
_DANGEROUS_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> str:
    """Return ``value`` as a CSV-safe string.

    Rules:
        * ``None`` -> empty string.
        * Non-string types are ``str(value)``-converted first.
        * If the resulting string starts with ``= + - @ \\t \\r``, a single
          quote ``'`` is prepended. The quote is the widely-recognized
          Excel "text literal" prefix — the cell renders normally for a
          human reader and is inert as a formula.

    Idempotency: ``_csv_safe(_csv_safe(x)) == _csv_safe(x)`` because the
    second application sees a leading ``'`` (safe character) and returns
    unchanged. This matters for double-escape bugs in pipelined exports.
    """
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    if s and s[0] in _DANGEROUS_PREFIXES:
        return "'" + s
    return s


def write_rows(
    header: Sequence[str], rows: Iterable[Sequence[Any]]
) -> str:
    """Render rows to a CSV string with every cell passed through ``_csv_safe``.

    The header is also sanitized (an attacker-controlled column name would
    otherwise bypass the defense). Uses the ``csv`` module's default dialect
    (comma + CRLF) so Excel opens it cleanly on Windows.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([_csv_safe(h) for h in header])
    for row in rows:
        writer.writerow([_csv_safe(cell) for cell in row])
    return buffer.getvalue()


__all__ = ["_csv_safe", "write_rows"]
