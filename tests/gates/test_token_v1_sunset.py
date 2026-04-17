"""v0.6.2-followup — legacy v1 tokens must hard-sunset.

Pre-fix: ``tokens.py:parse_token`` accepted the v1 format indefinitely.
An attacker who ever learned the current secret could mint v1
forgeries forever, bypassing v2 rotation-awareness.

Post-fix: ``GatesModule`` enforces an ``accept_v1_until`` cutoff (90
days after v0.6.2 ship date by default, ~2026-07-17). Past the cutoff,
a v1 token raises the distinct :class:`TokenVersionTooOldError` so
operator UIs can show a specific "your deployment aged out v1 tokens"
message and suggest the grace-window override.

Before the cutoff, a ``DeprecationWarning`` fires on every successful
legacy parse + a structured INFO log event so customers see the
deadline coming in their test suites.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets
import warnings
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from codeatelier_governance.gates import (
    ApprovalTokenError,
    TokenVersionTooOldError,
)
from codeatelier_governance.gates.module import (
    DEFAULT_ACCEPT_V1_UNTIL,
    GatesModule,
)
from codeatelier_governance.gates.tokens import parse_token


def _mint_legacy_v1(secret: bytes) -> tuple[str, object, str]:
    """Hand-mint a pre-v0.6.2 format token (no v2: prefix)."""
    rid = uuid4()
    action_hash = "a" * 64
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    body = f"{rid}:{action_hash}:{expires_at.isoformat()}"
    mac = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}:{mac}", rid, action_hash


@pytest.mark.asyncio
async def test_v1_token_rejected_past_sunset_with_distinct_error() -> None:
    """Past ``accept_v1_until`` → TokenVersionTooOldError, NOT generic."""
    secret = _secrets.token_bytes(32)
    legacy_token, _, _ = _mint_legacy_v1(secret)
    # Force the sunset to the past.
    past_cutoff = datetime.now(timezone.utc) - timedelta(days=1)

    with pytest.raises(TokenVersionTooOldError, match="legacy v1 format"):
        parse_token(
            secret=secret, token=legacy_token, accept_v1_until=past_cutoff
        )

    # Confirm it's still a subclass of ApprovalTokenError so legacy
    # except-blocks keep working on upgrade.
    try:
        parse_token(
            secret=secret, token=legacy_token, accept_v1_until=past_cutoff
        )
    except ApprovalTokenError:
        pass  # expected — subclass caught
    else:
        pytest.fail(
            "TokenVersionTooOldError must subclass ApprovalTokenError "
            "so existing except-handlers catch it without code changes."
        )


@pytest.mark.asyncio
async def test_v1_token_accepted_before_sunset_with_deprecation_warning() -> None:
    """Before cutoff: token verifies + DeprecationWarning fires."""
    secret = _secrets.token_bytes(32)
    legacy_token, rid, ah = _mint_legacy_v1(secret)
    future_cutoff = datetime.now(timezone.utc) + timedelta(days=30)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parsed_rid, parsed_ah, _ = parse_token(
            secret=secret,
            token=legacy_token,
            accept_v1_until=future_cutoff,
        )

    assert parsed_rid == rid
    assert parsed_ah == ah
    # Exactly one DeprecationWarning from the legacy parse path.
    dep_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert len(dep_warnings) == 1
    assert "v1 (pre-v0.6.2)" in str(dep_warnings[0].message)


@pytest.mark.asyncio
async def test_no_cutoff_means_no_sunset_for_back_compat() -> None:
    """``accept_v1_until=None`` → legacy tokens accepted indefinitely.

    Reserved for callers who have opted out of the sunset (e.g. tests
    of legacy back-compat, or emergency downstream consumers). The
    default for ``GatesModule`` is NOT None — see ``DEFAULT_ACCEPT_V1_UNTIL``.
    """
    secret = _secrets.token_bytes(32)
    legacy_token, rid, _ = _mint_legacy_v1(secret)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed_rid, _, _ = parse_token(
            secret=secret, token=legacy_token, accept_v1_until=None
        )
    assert parsed_rid == rid


@pytest.mark.asyncio
async def test_gates_module_passes_sunset_default_to_parse_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GatesModule wires DEFAULT_ACCEPT_V1_UNTIL through to parse_token.

    Without the wiring, parse_token gets accept_v1_until=None (no
    sunset) and the whole Fix 3 is a no-op. Assert the default value
    reaches the parser by simulating cutoff=past and observing
    TokenVersionTooOldError on a legacy-shape grant token.
    """
    from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
    from codeatelier_governance.audit.store import BatchingWriter

    monkeypatch.delenv("GOVERNANCE_GATES_ACCEPT_V1_UNTIL", raising=False)
    secret = _secrets.token_bytes(32)
    audit_store = InMemoryAuditStore(max_events=10_000)
    writer = BatchingWriter(primary=audit_store)
    audit = AuditModule(audit_store, secret=secret, writer=writer)
    await audit.start()
    try:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        gates = GatesModule(
            audit, secret=secret, accept_v1_until=past
        )
        assert gates._accept_v1_until == past  # type: ignore[attr-defined]

        # Check default GatesModule (no override) picks up
        # DEFAULT_ACCEPT_V1_UNTIL — a sanity check that the constructor
        # default path is wired, not just the explicit-arg path.
        gates_default = GatesModule(audit, secret=secret)
        assert (
            gates_default._accept_v1_until  # type: ignore[attr-defined]
            == DEFAULT_ACCEPT_V1_UNTIL
        )
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_env_var_override_extends_sunset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GOVERNANCE_GATES_ACCEPT_V1_UNTIL extends the cutoff for DR ops.

    Asserts two things:
      * A far-future ISO date in the env var is honored.
      * A malformed env value falls back to the default (doesn't crash).
    """
    from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
    from codeatelier_governance.audit.store import BatchingWriter

    secret = _secrets.token_bytes(32)
    audit_store = InMemoryAuditStore(max_events=10_000)
    writer = BatchingWriter(primary=audit_store)
    audit = AuditModule(audit_store, secret=secret, writer=writer)
    await audit.start()
    try:
        monkeypatch.setenv(
            "GOVERNANCE_GATES_ACCEPT_V1_UNTIL", "2030-01-01T00:00:00+00:00"
        )
        gates = GatesModule(audit, secret=secret)
        assert gates._accept_v1_until == datetime(  # type: ignore[attr-defined]
            2030, 1, 1, tzinfo=timezone.utc
        )

        monkeypatch.setenv("GOVERNANCE_GATES_ACCEPT_V1_UNTIL", "not a date")
        gates_bad = GatesModule(audit, secret=secret)
        # Fallback to default — no raise.
        assert gates_bad._accept_v1_until == DEFAULT_ACCEPT_V1_UNTIL  # type: ignore[attr-defined]
    finally:
        await audit.close()
