"""BLOCKER C5: metadata sanitization at the audit layer.

The HTTP-layer ``HaltRequest._sanitize_reason`` validator only fires on
the ``reason`` field at the API boundary. Any caller that reaches into
``AuditModule.log`` programmatically previously bypassed it entirely —
an insider with SDK creds could forge an ANSI escape sequence into the
audit chain and paint an operator's terminal at log-export time.

These tests pin that the shared sanitizer in
``codeatelier_governance.audit.sanitization`` is applied recursively to
every string leaf in ``AuditEvent.metadata``.
"""
from __future__ import annotations

from uuid import uuid4


from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.audit.sanitization import (
    HALT_REASON_MAX_LEN,
    sanitize_string,
)
from codeatelier_governance.console.app import HaltRequest


def test_programmatic_log_strips_ansi_escapes() -> None:
    """An ANSI escape forged into metadata must be stripped before storage."""
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        session_id=uuid4(),
        metadata={"reason": "\x1b[2J\x1b[H[FAKE]"},
    )
    assert "\x1b" not in evt.metadata["reason"]
    # The bracket characters survive — they are normal printable text.
    assert "[FAKE]" in evt.metadata["reason"]


def test_metadata_nested_dict_sanitized_recursively() -> None:
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        session_id=uuid4(),
        metadata={
            "outer": {
                "inner": "\x1b[31mred\x1b[0m",
                "deep": {"deeper": "\x00null"},
            }
        },
    )
    assert "\x1b" not in evt.metadata["outer"]["inner"]
    assert "\x00" not in evt.metadata["outer"]["deep"]["deeper"]


def test_metadata_list_values_sanitized() -> None:
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        session_id=uuid4(),
        metadata={"list": ["\x1b[2Jclean", "ok\x07also"]},
    )
    for item in evt.metadata["list"]:
        assert "\x1b" not in item
        assert "\x07" not in item


def test_kill_request_and_audit_event_use_same_sanitizer() -> None:
    """Same input through both surfaces must produce the same string."""
    raw = "a\\nb\nc\x1b[31mred"
    via_halt = HaltRequest(reason=raw).reason
    via_helper = sanitize_string(raw, max_len=HALT_REASON_MAX_LEN)
    assert via_halt == via_helper


def test_nfc_normalization_applied() -> None:
    """A combining-mark decomposition must be NFC-composed in stored metadata."""
    raw = "e\u0301"  # decomposed e + combining acute
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        session_id=uuid4(),
        metadata={"name": raw},
    )
    assert "\u0301" not in evt.metadata["name"]
    assert evt.metadata["name"] == "\u00e9"


def test_escape_order_backslash_first() -> None:
    """``a\\nb\\nc`` (literal backslash-n then real newline) must escape
    the literal backslash FIRST so the real newline does not collide."""
    raw = "a\\nb\nc"  # 'a','\\','n','b','\n','c'
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        session_id=uuid4(),
        metadata={"reason": raw},
    )
    assert evt.metadata["reason"] == "a\\\\nb\\nc"
