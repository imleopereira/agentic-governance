"""Tests for the formula-injection-safe CSV export helper.

OWASP CSV Injection reference: cells starting with ``= + - @`` — and tab
or CR characters — are interpreted as formulas by Excel / Sheets /
LibreOffice. Prefix-quoting renders the cell inert without data loss.
"""
from __future__ import annotations

import csv
import io

from codeatelier_governance.cost.csv_export import _csv_safe, write_rows


def test_equals_prefix_gets_quoted() -> None:
    assert _csv_safe("=HYPERLINK(\"evil.com\", \"click\")") == (
        "'=HYPERLINK(\"evil.com\", \"click\")"
    )


def test_plus_prefix_gets_quoted() -> None:
    assert _csv_safe("+CMD|'calc'!A0") == "'+CMD|'calc'!A0"


def test_minus_prefix_gets_quoted() -> None:
    # Leading '-' would be re-evaluated as formula in some parsers.
    assert _csv_safe("-2+3+cmd|'calc'!A0") == "'-2+3+cmd|'calc'!A0"


def test_at_prefix_gets_quoted() -> None:
    assert _csv_safe("@SUM(1+1)") == "'@SUM(1+1)"


def test_tab_prefix_gets_quoted() -> None:
    assert _csv_safe("\t=2+2") == "'\t=2+2"


def test_cr_prefix_gets_quoted() -> None:
    assert _csv_safe("\r=2+2") == "'\r=2+2"


def test_benign_passthrough() -> None:
    assert _csv_safe("billing-agent-1") == "billing-agent-1"
    assert _csv_safe("hello world") == "hello world"
    assert _csv_safe("user@example.com") == "user@example.com"  # @ not leading
    assert _csv_safe("3 + 4 = 7") == "3 + 4 = 7"  # leading digit


def test_none_becomes_empty_string() -> None:
    assert _csv_safe(None) == ""


def test_numbers_are_str_converted_and_safe() -> None:
    assert _csv_safe(42) == "42"
    assert _csv_safe(3.14) == "3.14"


def test_idempotent() -> None:
    # Double-escape must not double-prefix.
    once = _csv_safe("=evil()")
    twice = _csv_safe(once)
    assert once == twice == "'=evil()"


def test_write_rows_sanitizes_header_and_cells() -> None:
    out = write_rows(
        header=["agent_id", "=COUNT(A:A)", "usd"],
        rows=[
            ["billing", "=HYPERLINK(\"evil.com\")", 4.82],
            ["+calc", "ok", 1.0],
            ["@danger", "=1+1", 0.5],
        ],
    )
    reader = list(csv.reader(io.StringIO(out)))
    # Header: first col passes through, second is neutralized
    assert reader[0][0] == "agent_id"
    assert reader[0][1].startswith("'=")
    # All evil rows neutralized
    assert reader[1][1].startswith("'=")
    assert reader[2][0].startswith("'+")
    assert reader[3][0].startswith("'@")
    assert reader[3][1].startswith("'=")
    # Benign USD preserved
    assert reader[1][2] == "4.82"
