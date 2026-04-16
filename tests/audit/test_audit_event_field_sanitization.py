"""S5 P1 #1: every string field on ``AuditEvent`` is sanitized.

Only ``metadata`` previously ran through ``sanitize_metadata``. The other
caller-controllable string fields — ``kind``, ``agent_id``, ``model``,
``input_hash``, ``output_hash`` — accepted raw C0/ANSI bytes and carried
them through the HMAC chain into ``AuditEventView`` responses, the
operator ``governance audit tail`` CLI, and compliance exports. That is
an insider-with-SDK-creds text-injection primitive against any tool
that renders audit rows in a terminal or HTML surface.

These tests pin that the same ``sanitize_string`` helper now processes
every ``str`` field on construction. Non-string fields (UUIDs, datetimes,
bytes signatures) remain untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from codeatelier_governance.audit.models import (
    MAX_AGENT_ID_LEN,
    MAX_HASH_LEN,
    MAX_KIND_LEN,
    AuditEvent,
)
from codeatelier_governance.audit.sanitization import sanitize_string


# --- Per-field C0/ANSI stripping ----------------------------------------------


def test_kind_ansi_escape_stripped() -> None:
    """S5 P1 #1 headline case: ``kind`` no longer ferries ANSI escapes."""
    evt = AuditEvent(
        agent_id="agent-1",
        kind="scope.violation\x1b[2J\x1b[H",
        session_id=uuid4(),
        metadata={},
    )
    assert "\x1b" not in evt.kind
    # The literal bracket text survives — it's ordinary printable content.
    # What matters is that \x1b is gone so no terminal can interpret it.
    assert evt.kind.startswith("scope.violation")


def test_agent_id_c0_stripped() -> None:
    evt = AuditEvent(
        agent_id="agent\x00-1\x07",
        kind="test",
        session_id=uuid4(),
        metadata={},
    )
    assert "\x00" not in evt.agent_id
    assert "\x07" not in evt.agent_id
    assert "agent" in evt.agent_id
    assert "-1" in evt.agent_id


def test_model_ansi_escape_stripped() -> None:
    evt = AuditEvent(
        agent_id="a",
        kind="llm.call",
        model="gpt-4\x1b[31m",
        session_id=uuid4(),
        metadata={},
    )
    assert evt.model is not None
    assert "\x1b" not in evt.model
    assert "gpt-4" in evt.model


def test_input_hash_c0_stripped() -> None:
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        input_hash="deadbeef\x1b[2J",
        session_id=uuid4(),
        metadata={},
    )
    assert evt.input_hash is not None
    assert "\x1b" not in evt.input_hash
    assert "deadbeef" in evt.input_hash


def test_output_hash_c0_stripped() -> None:
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        output_hash="cafebabe\x00",
        session_id=uuid4(),
        metadata={},
    )
    assert evt.output_hash is not None
    assert "\x00" not in evt.output_hash
    assert "cafebabe" in evt.output_hash


# --- Behavior matches sanitize_string exactly ---------------------------------


@pytest.mark.parametrize(
    "field_name,max_len",
    [
        ("agent_id", MAX_AGENT_ID_LEN),
        ("kind", MAX_KIND_LEN),
        ("model", 128),
        ("input_hash", MAX_HASH_LEN),
        ("output_hash", MAX_HASH_LEN),
    ],
)
def test_field_matches_sanitize_string_contract(field_name: str, max_len: int) -> None:
    """Each field produces the same output as a direct ``sanitize_string`` call."""
    raw = "payload\x1b[31mred\x1b[0m\nnext"
    kwargs: dict[str, object] = {
        "agent_id": "a",
        "kind": "test",
        "session_id": uuid4(),
        "metadata": {},
    }
    kwargs[field_name] = raw
    evt = AuditEvent(**kwargs)  # type: ignore[arg-type]
    actual = getattr(evt, field_name)
    expected = sanitize_string(raw, max_len=max_len)
    assert actual == expected


# --- NFC normalization --------------------------------------------------------


def test_nfc_normalization_kind_and_agent_id() -> None:
    """Decomposed and precomposed forms collapse to the same output."""
    decomposed = "cafe\u0301"  # 'cafe' + combining acute -> 'café'
    precomposed = "caf\u00e9"
    evt_d = AuditEvent(
        agent_id=decomposed,
        kind=decomposed,
        session_id=uuid4(),
        metadata={},
    )
    evt_p = AuditEvent(
        agent_id=precomposed,
        kind=precomposed,
        session_id=uuid4(),
        metadata={},
    )
    assert evt_d.agent_id == evt_p.agent_id == precomposed
    assert evt_d.kind == evt_p.kind == precomposed
    # Decomposed combining mark is gone.
    assert "\u0301" not in evt_d.agent_id
    assert "\u0301" not in evt_d.kind


# --- Length caps --------------------------------------------------------------


def test_sanitize_string_respects_hash_len_cap() -> None:
    """The sanitizer cap never lets a hash field exceed MAX_HASH_LEN.

    Pydantic's max_length runs on the raw input. NFC composition can
    occasionally shrink strings (combining-mark merges), so the real
    cap is Pydantic's. What we assert here is that once the field has
    passed Pydantic, the sanitizer never *grows* it past the cap via
    escape expansion — the sanitizer caps input length before escaping.
    """
    # 128-char hash with no specials — must pass through unchanged.
    raw = "a" * MAX_HASH_LEN
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        input_hash=raw,
        session_id=uuid4(),
        metadata={},
    )
    assert evt.input_hash == raw
    assert evt.input_hash is not None
    assert len(evt.input_hash) == MAX_HASH_LEN


def test_backslash_in_kind_is_escaped() -> None:
    """Backslash escaping rule from sanitize_string applies to ``kind`` too."""
    evt = AuditEvent(
        agent_id="a",
        kind="a\\nb",  # literal backslash-n, not a newline
        session_id=uuid4(),
        metadata={},
    )
    # sanitize_string replaces backslash with backslash-backslash.
    assert evt.kind == "a\\\\nb"


# --- Non-string fields are NOT touched ----------------------------------------


def test_uuid_fields_untouched() -> None:
    sid = uuid4()
    pid = uuid4()
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        session_id=sid,
        parent_event_id=pid,
        metadata={},
    )
    assert evt.session_id == sid
    assert evt.parent_event_id == pid
    assert isinstance(evt.session_id, UUID)
    assert isinstance(evt.parent_event_id, UUID)


def test_none_string_fields_stay_none() -> None:
    """Optional string fields left as None must not become empty strings."""
    evt = AuditEvent(
        agent_id="a",
        kind="test",
        session_id=uuid4(),
        metadata={},
    )
    assert evt.model is None
    assert evt.input_hash is None
    assert evt.output_hash is None


# --- Round-trip serialization -------------------------------------------------


def test_model_dump_contains_no_raw_ansi_or_c0() -> None:
    """Full round-trip: build AuditEvent, dump to dict, verify no raw bytes."""
    evt = AuditEvent(
        agent_id="agent\x07one",
        kind="scope.violation\x1b[2J\x1b[H",
        model="gpt-4\x1b[31m",
        input_hash="deadbeef\x00",
        output_hash="cafebabe\x1b[0m",
        session_id=uuid4(),
        metadata={"note": "forge\x1b[2J"},
    )
    dumped = evt.model_dump()
    # Spot-check the string fields in the dump.
    for key in ("agent_id", "kind", "model", "input_hash", "output_hash"):
        value = dumped[key]
        assert isinstance(value, str)
        for bad_byte in ("\x00", "\x01", "\x07", "\x1b", "\x7f"):
            assert bad_byte not in value, f"{key} still contains {bad_byte!r}"
    # Metadata string leaf also clean.
    assert "\x1b" not in dumped["metadata"]["note"]


def test_round_trip_via_dict_reconstruct() -> None:
    """Dumping and rebuilding must converge — the sanitized form is a fixed point."""
    evt = AuditEvent(
        agent_id="a\x00b",
        kind="k\x1b[31m",
        session_id=uuid4(),
        metadata={},
    )
    dumped = evt.model_dump()
    rebuilt = AuditEvent.model_validate(dumped)
    assert rebuilt.agent_id == evt.agent_id
    assert rebuilt.kind == evt.kind


# --- Datetime / non-str type not accidentally coerced --------------------------


def test_utc_datetime_is_not_a_string_field() -> None:
    """Sanity: construction never needs ``created_at`` — that's on the Record."""
    # AuditEvent doesn't carry created_at; this test documents that the
    # sanitizer scope is limited to AuditEvent's actual str fields.
    now = datetime.now(tz=timezone.utc)
    assert isinstance(now, datetime)
    # AuditEvent has no 'created_at' / 'event_id' — only AuditEventRecord does.
    assert "created_at" not in AuditEvent.model_fields
    assert "event_id" not in AuditEvent.model_fields
