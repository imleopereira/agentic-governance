"""Tests for the v0.6 audit-correction allowlist enforcement module.

See ``codeatelier_governance.audit.corrections`` for the full threat model.
Each test here maps back to one cell in the threat matrix in that docstring.
"""
from __future__ import annotations

import secrets

import pytest

from codeatelier_governance.audit import (
    ALLOWED_REASONS,
    NOTE_MAX_CHARS,
    CorrectionValidationError,
    validate_correction_payload,
)
from codeatelier_governance.audit.corrections import (
    PERMITTED_KINDS,
    hash_commit_sha,
)

# Deterministic but high-entropy key. The corrections module rejects keys
# with < 8 distinct bytes (e.g. ``b"k" * 32``) to catch placeholder-secret
# leaks; the test suite uses ``secrets.token_bytes`` so every run gets a
# real key that passes the entropy floor.
_HMAC_KEY = secrets.token_bytes(32)
_HMAC_KEY_ALT = secrets.token_bytes(32)


def _base_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "audit.correction",
        "reason": "operator_marked_erroneous",
        "operator_id": "leo@codeatelier",
        "ref_chain_seq_start": 100,
        "ref_chain_seq_end": 150,
        "note": "Retraction of erroneous audit rows from 2026-04-14.",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_happy_path_returns_sanitized_dict() -> None:
    out = validate_correction_payload(_base_payload(), _HMAC_KEY)
    assert out["kind"] == "audit.correction"
    assert out["reason"] == "operator_marked_erroneous"
    assert out["operator_id"] == "leo@codeatelier"
    assert out["ref_chain_seq_start"] == 100
    assert out["ref_chain_seq_end"] == 150
    assert isinstance(out["note"], str)


def test_permitted_kinds_is_only_audit_correction() -> None:
    assert PERMITTED_KINDS == frozenset({"audit.correction"})


# ---------------------------------------------------------------------------
# Kind enforcement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_kind",
    ["system.override", "audit.rewrite", "audit.correction.v2", "", "approval.granted"],
)
def test_non_permitted_kinds_are_rejected(bad_kind: str) -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(_base_payload(kind=bad_kind), _HMAC_KEY)
    assert exc.value.field == "kind"


def test_missing_kind_is_rejected() -> None:
    payload = _base_payload()
    del payload["kind"]
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(payload, _HMAC_KEY)
    assert exc.value.field == "kind"


# ---------------------------------------------------------------------------
# Forbidden path patterns in the note
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "note",
    [
        "/Users/leo/secret.py",
        "/srv/gov/cron.sh",
        "see /home/x/bashrc",
        "/var/log/audit",
        "/tmp/file.sql",
        "/opt/governance/prod",
    ],
)
def test_forbidden_filesystem_paths_are_rejected(note: str) -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(_base_payload(note=note), _HMAC_KEY)
    assert exc.value.field == "note"


@pytest.mark.parametrize(
    "note",
    [
        "fix in script.py",
        "run fix.sh",
        "query.sql needs update",
        "cron misfire",
        "the cronjob ran twice",
        "wrong branch",
        "console-redesign retraction",
    ],
)
def test_forbidden_infra_tokens_are_rejected(note: str) -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(_base_payload(note=note), _HMAC_KEY)
    assert exc.value.field == "note"


def test_raw_git_sha_in_note_is_rejected() -> None:
    # Real short SHA from this repo's history (v0.5.3 tag).
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(
            _base_payload(note="regression since 81b5912"),
            _HMAC_KEY,
        )
    assert exc.value.field == "note"


def test_hashed_commit_sha_token_is_accepted() -> None:
    hashed = hash_commit_sha("81b5912abcdef0123456", _HMAC_KEY)
    # The hashed form is `sha256-<12hex>`; the hex is NOT 7+ standalone chars
    # because the `sha256-` prefix breaks the word boundary.
    note = f"retraction; ref {hashed}"
    out = validate_correction_payload(_base_payload(note=note), _HMAC_KEY)
    assert hashed in out["note"]


def test_ipv4_in_note_is_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(
            _base_payload(note="traffic from 10.0.0.5"),
            _HMAC_KEY,
        )
    assert exc.value.field == "note"


def test_html_tag_in_note_is_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(
            _base_payload(note="<script>alert(1)</script>"),
            _HMAC_KEY,
        )
    assert exc.value.field == "note"


# ---------------------------------------------------------------------------
# Note length boundary
# ---------------------------------------------------------------------------
def test_note_exactly_max_chars_passes() -> None:
    # 256 of a benign char (no hex, no paths, no tags).
    note = "z" * NOTE_MAX_CHARS
    out = validate_correction_payload(_base_payload(note=note), _HMAC_KEY)
    assert len(out["note"]) == NOTE_MAX_CHARS


def test_note_over_max_chars_rejected() -> None:
    note = "z" * (NOTE_MAX_CHARS + 1)
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(_base_payload(note=note), _HMAC_KEY)
    assert exc.value.field == "note"


def test_empty_note_allowed() -> None:
    out = validate_correction_payload(_base_payload(note=""), _HMAC_KEY)
    assert out["note"] == ""


def test_unicode_note_accepted() -> None:
    out = validate_correction_payload(
        _base_payload(note="retratação concluída — rápido"),
        _HMAC_KEY,
    )
    assert "retratação" in out["note"]


def test_unicode_with_forbidden_token_still_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(
            _base_payload(note="correção do cron diário"),
            _HMAC_KEY,
        )
    assert exc.value.field == "note"


# ---------------------------------------------------------------------------
# Field allowlist
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "extra_key,extra_val",
    [
        ("db_pool_size", 5),
        ("internal_hostname", "ops-1"),
        ("stack_trace", "Traceback..."),
        ("asyncpg_version", "0.29"),
    ],
)
def test_unknown_field_is_rejected(extra_key: str, extra_val: object) -> None:
    payload = _base_payload()
    payload[extra_key] = extra_val
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(payload, _HMAC_KEY)
    assert exc.value.field == extra_key


# ---------------------------------------------------------------------------
# Reason enum
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("reason", sorted(ALLOWED_REASONS))
def test_each_allowed_reason_accepted(reason: str) -> None:
    out = validate_correction_payload(_base_payload(reason=reason), _HMAC_KEY)
    assert out["reason"] == reason


def test_free_text_reason_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(
            _base_payload(reason="because I said so"),
            _HMAC_KEY,
        )
    assert exc.value.field == "reason"


# ---------------------------------------------------------------------------
# operator_id & seq bounds
# ---------------------------------------------------------------------------
def test_empty_operator_id_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(_base_payload(operator_id="   "), _HMAC_KEY)
    assert exc.value.field == "operator_id"


def test_negative_seq_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(
            _base_payload(ref_chain_seq_start=-1), _HMAC_KEY
        )
    assert exc.value.field == "ref_chain_seq_start"


def test_end_before_start_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(
            _base_payload(ref_chain_seq_start=200, ref_chain_seq_end=100),
            _HMAC_KEY,
        )
    assert exc.value.field == "ref_chain_seq_end"


def test_bool_seq_rejected_even_though_bool_is_int_subclass() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(
            _base_payload(ref_chain_seq_start=True), _HMAC_KEY  # type: ignore[arg-type]
        )
    assert exc.value.field == "ref_chain_seq_start"


# ---------------------------------------------------------------------------
# Payload-level failures
# ---------------------------------------------------------------------------
def test_empty_payload_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload({}, _HMAC_KEY)
    assert exc.value.field == "payload"


def test_non_dict_payload_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload("hello", _HMAC_KEY)  # type: ignore[arg-type]
    assert exc.value.field == "payload"


def test_short_hmac_key_rejected() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(_base_payload(), b"short")
    assert exc.value.field == "hmac_key"


# ---------------------------------------------------------------------------
# SQL-injection attempt: accepted as raw text
# ---------------------------------------------------------------------------
def test_sql_injection_note_is_accepted_as_text() -> None:
    """The audit write path parameterizes every query (asyncpg binds). A
    literal SQL-looking string in ``note`` is safe to store and must not
    trip the allowlist. This test documents that assumption.
    """
    note = "'; DROP TABLE governance_audit_events; --"
    out = validate_correction_payload(_base_payload(note=note), _HMAC_KEY)
    assert out["note"] == note


# ---------------------------------------------------------------------------
# hash_commit_sha
# ---------------------------------------------------------------------------
def test_hash_commit_sha_deterministic_for_same_key() -> None:
    a = hash_commit_sha("81b5912", _HMAC_KEY)
    b = hash_commit_sha("81b5912", _HMAC_KEY)
    assert a == b


def test_hash_commit_sha_varies_with_key() -> None:
    a = hash_commit_sha("81b5912", _HMAC_KEY)
    b = hash_commit_sha("81b5912", _HMAC_KEY_ALT)
    assert a != b


def test_hash_commit_sha_format() -> None:
    out = hash_commit_sha("81b5912abcdef", _HMAC_KEY)
    assert out.startswith("sha256-")
    tail = out.split("-", 1)[1]
    assert len(tail) == 12
    assert all(c in "0123456789abcdef" for c in tail)


def test_hash_commit_sha_rejects_empty_sha() -> None:
    with pytest.raises(CorrectionValidationError):
        hash_commit_sha("", _HMAC_KEY)


def test_hash_commit_sha_rejects_short_key() -> None:
    with pytest.raises(CorrectionValidationError):
        hash_commit_sha("81b5912", b"short")


# ---------------------------------------------------------------------------
# Exception surface
# ---------------------------------------------------------------------------
def test_exception_exposes_field_reason_and_hint() -> None:
    try:
        validate_correction_payload(_base_payload(kind="bad"), _HMAC_KEY)
    except CorrectionValidationError as exc:
        assert exc.field == "kind"
        assert exc.reason
        assert exc.operator_hint
    else:
        pytest.fail("CorrectionValidationError not raised")


def test_exception_str_contains_operator_hint() -> None:
    """A refactor must not silently drop operator_hint from ``__str__``.

    The CLI surfaces ``str(exc)`` to the operator — losing the hint there
    would turn a fix-in-30-seconds error into a pager ping.
    """
    with pytest.raises(CorrectionValidationError) as exc_info:
        validate_correction_payload({"kind": "wrong.kind"}, hmac_key=_HMAC_KEY)
    assert exc_info.value.operator_hint in str(exc_info.value)


# ---------------------------------------------------------------------------
# H1 — SHA look-behind hardening
# ---------------------------------------------------------------------------
def test_xsha256_prefix_bypass_is_rejected() -> None:
    """A raw SHA glued to a non-word prefix (``xsha256-...``) must fail.

    The earlier look-behind accepted any character before ``sha256-`` as
    "not hex, therefore sanctioned hashed token", which let real hex
    payloads slip through if an attacker prepended a single character.
    The tightened pattern requires ``sha256-`` to sit at a word boundary.
    """
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(
            _base_payload(note="see commit xsha256-deadbeef1234"),
            _HMAC_KEY,
        )
    assert exc.value.field == "note"


# ---------------------------------------------------------------------------
# H2 — HMAC key entropy floor
# ---------------------------------------------------------------------------
def test_low_entropy_hmac_key_rejected_in_validate() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        validate_correction_payload(_base_payload(), b"k" * 32)
    assert exc.value.field == "hmac_key"
    assert "low_entropy" in exc.value.reason or "entropy" in exc.value.operator_hint.lower()


def test_low_entropy_hmac_key_rejected_in_hash_commit_sha() -> None:
    with pytest.raises(CorrectionValidationError) as exc:
        hash_commit_sha("81b5912", b"\x00" * 32)
    assert exc.value.field == "hmac_key"


# ---------------------------------------------------------------------------
# B3 — Second-layer guard on AuditModule.log for audit.correction
# ---------------------------------------------------------------------------
async def test_direct_sdk_audit_log_correction_raises(audit: object) -> None:
    """``sdk.audit.log(AuditEvent(kind='audit.correction'))`` must be refused.

    The first-layer allowlist is only invoked by code paths that choose
    to call it. Any code path that constructs an ``AuditEvent`` directly
    would otherwise bypass every field / reason / pattern check. The
    second-layer guard in ``AuditModule.log`` raises RuntimeError to
    prevent that.
    """
    from codeatelier_governance.audit.models import AuditEvent

    with pytest.raises(RuntimeError, match="log_validated_correction"):
        await audit.log(  # type: ignore[attr-defined]
            AuditEvent(
                agent_id="sneaky",
                kind="audit.correction",
                metadata={"note": "/Users/leo/secret.py"},
            )
        )


async def test_log_validated_correction_happy_path(audit: object) -> None:
    """The sanctioned path writes a correction event successfully."""
    sanitized = validate_correction_payload(_base_payload(), _HMAC_KEY)
    record = await audit.log_validated_correction(  # type: ignore[attr-defined]
        sanitized, _HMAC_KEY
    )
    assert record.kind == "audit.correction"
    assert record.metadata["reason"] == "operator_marked_erroneous"
    assert record.metadata["operator_id"] == "leo@codeatelier"


async def test_log_validated_correction_rejects_unsanitized_kind(
    audit: object,
) -> None:
    """A raw dict with a non-correction kind is rejected before reaching log."""
    with pytest.raises(RuntimeError, match="allowlist"):
        await audit.log_validated_correction(  # type: ignore[attr-defined]
            {"kind": "system.override"}, _HMAC_KEY
        )
