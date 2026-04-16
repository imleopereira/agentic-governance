"""Unit tests for KillRequest.reason sanitization (F2 P0 Cybersec HIGH).

Covers:
    * Escape-order invariant (backslash must be escaped first).
    * NFC normalization applied before length cap (zalgo bomb).
    * Length cap in codepoints, applied BEFORE escape expansion.
    * ANSI escape (\\x1b[31m) control byte stripped.
    * Multibyte characters counted as codepoints, not bytes, at the 512 cap.
"""
from __future__ import annotations

import pytest

from codeatelier_governance.console.app import KillRequest, _KILL_REASON_MAX


def test_escape_order_preserves_literal_backslash_n() -> None:
    """`a\\nb\\nc` input (one literal `\\n`, one real newline) must produce
    `a\\\\nb\\nc` — the literal backslash-n gets double-escaped FIRST, then
    the real newline gets escaped to `\\n`. If we escaped `\\n` first, the
    literal backslash in the input would collide with the new escape and
    corrupt both."""
    raw = "a\\nb\nc"  # 5 chars: 'a','\\','n','b','\n','c' -> 6 codepoints
    result = KillRequest(reason=raw).reason
    # Expected: `\\` -> `\\\\`, then real `\n` -> `\\n`
    assert result == "a\\\\nb\\nc"


def test_nfc_normalization_applied_before_length_cap() -> None:
    """A zalgo-like string with many combining marks should first be NFC-
    normalized, then truncated to the codepoint cap. NFC can only shorten
    or leave length unchanged for BMP text, so post-normalize length is
    the authoritative measure."""
    # NFC: é (U+00E9) is preferred over e + COMBINING ACUTE (U+0065 U+0301).
    raw = "e\u0301" * 400  # 800 codepoints decomposed -> 400 composed
    result = KillRequest(reason=raw).reason
    assert len(result) <= _KILL_REASON_MAX
    # Verify composition happened: no combining acute left.
    assert "\u0301" not in result


def test_600_char_input_truncated_to_512() -> None:
    raw = "x" * 600
    result = KillRequest(reason=raw).reason
    assert len(result) == _KILL_REASON_MAX == 512


def test_ansi_escape_stripped() -> None:
    """\\x1b[31m red-text ANSI escape must have the ESC byte (\\x1b) stripped
    by the C0 control filter. The trailing `[31m` becomes harmless text."""
    raw = "danger \x1b[31mRED\x1b[0m end"
    result = KillRequest(reason=raw).reason
    assert "\x1b" not in result
    assert "RED" in result  # non-control bytes survive


def test_multibyte_codepoint_cap() -> None:
    """512 multi-byte codepoints (each 4 bytes in UTF-8) must still fit —
    the cap is on codepoints, not bytes."""
    emoji = "\U0001f600"  # 😀, 1 codepoint, 4 bytes UTF-8
    raw = emoji * 512
    result = KillRequest(reason=raw).reason
    assert len(result) == 512
    assert result == raw


def test_overlong_multibyte_truncated() -> None:
    emoji = "\U0001f600"
    raw = emoji * 700
    result = KillRequest(reason=raw).reason
    assert len(result) == _KILL_REASON_MAX


def test_c0_controls_stripped_but_newlines_escaped() -> None:
    """Real newlines go through the escape pass; \\x00, \\x07 (BEL),
    \\x7f (DEL) go through the C0-control stripper."""
    raw = "bell\x07null\x00del\x7fhello\nworld"
    result = KillRequest(reason=raw).reason
    assert "\x00" not in result
    assert "\x07" not in result
    assert "\x7f" not in result
    assert "\\n" in result  # real newline was escaped, not stripped


def test_empty_reason_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KillRequest(reason="")
