"""v0.6.2 P0 — grant/deny tokens must be rotation-aware.

Pre-v0.6.2 the gates secret lived in :class:`GatesModule` as a single
``self._secret`` field. :func:`make_token` and :func:`parse_token` both
used it directly. After an operator ran ``cga rotate-chain-key`` — or
simply rotated the gates secret env var — every LAWFULLY-MINTED
pre-rotation token started failing the current-secret HMAC check and
was rejected with ``signature mismatch``. Because the gates secret is
commonly the same env var as the audit secret (recommended deployment
in the docs), this was an identical-shape sibling to the session-verify
rotation P0 already fixed in commit ed66c71.

v0.6.2 embeds the 16-char salted-fingerprint prefix of the signing key
into the token body (``v2:<prefix>:...``). On parse, the verifier
resolves the prefix to raw key bytes via the same
``GOVERNANCE_CHAIN_KEY_<prefix>`` env-var protocol used by the audit
chain verifier, then verifies the HMAC under the resolved key. When
the historical key material is not mounted, a DISTINCT ``historical
key unavailable`` error is raised so operators can tell "forgot to
mount the rotated-away key" apart from "token was tampered with".

This file is the dedicated regression for that fix. The happy-path
legacy tests live in :mod:`tests.gates.test_gates`.
"""
from __future__ import annotations

import base64
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from codeatelier_governance.audit.keys import (
    clear_key_cache,
    fingerprint_key,
)
from codeatelier_governance.gates import ApprovalTokenError, GatesModule
from codeatelier_governance.gates.store import InMemoryGatesStore
from codeatelier_governance.gates.tokens import (
    KEY_PREFIX_LEN,
    TOKEN_VERSION_V2,
    make_token,
    parse_token,
)


# ---------------------------------------------------------------------------
# Rotation — pre-rotation token verifies under mounted historical key
# ---------------------------------------------------------------------------


def _fresh_secret() -> bytes:
    """Strong random secret satisfying GatesModule's strength check."""
    return _secrets.token_bytes(32)


def _prefix_env_name(secret: bytes) -> str:
    """Env var name the operator would set after rotating away ``secret``."""
    return f"GOVERNANCE_CHAIN_KEY_{fingerprint_key(secret)[:KEY_PREFIX_LEN]}"


@pytest.fixture(autouse=True)
def _isolate_key_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the audit.keys LRU between tests so historical-key lookups
    do not leak between test cases. The cache caches negative results
    so a test that sets an env var after a miss would otherwise see a
    stale UNAVAILABLE reading."""
    clear_key_cache()
    yield
    clear_key_cache()
    # Ensure any GOVERNANCE_CHAIN_KEY_* env vars set via os.environ
    # directly are cleared too, in case a test forgot monkeypatch.
    import os

    for key in list(os.environ.keys()):
        if key.startswith("GOVERNANCE_CHAIN_KEY_"):
            del os.environ[key]


@pytest.mark.asyncio
async def test_token_minted_under_v1_verifies_after_rotation_to_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mint under secret v1, rotate to v2, grant with the v1 token.

    This is the bug the spec calls out verbatim: "mint a token under
    secret v1, rotate to v2, call grant with the v1 token. MUST succeed
    with verified=true." We build two :class:`GatesModule` instances
    that share an :class:`InMemoryGatesStore` so the pending row
    persists across the "rotation" (rebuild-of-module under new
    secret).
    """
    from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
    from codeatelier_governance.audit.store import BatchingWriter

    secret_v1 = _fresh_secret()
    secret_v2 = _fresh_secret()
    # Shared store spans the rotation — the pending row must survive.
    store = InMemoryGatesStore()

    # --- 1. Pre-rotation: agent requests under v1, token is minted. ---
    audit_store_1 = InMemoryAuditStore(max_events=10_000)
    writer_1 = BatchingWriter(primary=audit_store_1)
    audit_1 = AuditModule(audit_store_1, secret=secret_v1, writer=writer_1)
    await audit_1.start()
    try:
        # v0.6.2-followup: v2 token minting is now opt-in (default
        # False for downgrade safety during rolling deploys). This
        # test covers the rotation-aware PATH, so it must explicitly
        # opt in. The downgrade-safety story is tested separately in
        # test_token_downgrade_safety.py.
        gates_v1 = GatesModule(
            audit_1, secret=secret_v1, store=store, enable_v2_tokens=True
        )
        req = await gates_v1.request(
            "delete.patient", "agent-alpha", payload={"id": 42}
        )
    finally:
        await audit_1.close()

    v1_token = req.token
    # Assert the embedded prefix is v1's, not v2's.
    assert v1_token.startswith(f"{TOKEN_VERSION_V2}:")
    embedded_prefix = v1_token.split(":")[1]
    assert embedded_prefix == fingerprint_key(secret_v1)[:KEY_PREFIX_LEN]
    # Sanity: that's NOT v2's prefix.
    assert embedded_prefix != fingerprint_key(secret_v2)[:KEY_PREFIX_LEN]

    # --- 2. Operator rotates: the secret is now v2. To verify the v1
    # token they must mount v1 material under GOVERNANCE_CHAIN_KEY_<v1-prefix>.
    monkeypatch.setenv(
        _prefix_env_name(secret_v1),
        "env://OLD_GATES_SECRET",
    )
    monkeypatch.setenv(
        "OLD_GATES_SECRET",
        base64.b64encode(secret_v1).decode("ascii"),
    )

    # --- 3. Post-rotation grant: build a NEW GatesModule under v2 that
    # shares the same store. Grant with the v1-minted token.
    audit_store_2 = InMemoryAuditStore(max_events=10_000)
    writer_2 = BatchingWriter(primary=audit_store_2)
    audit_2 = AuditModule(audit_store_2, secret=secret_v2, writer=writer_2)
    await audit_2.start()
    try:
        gates_v2 = GatesModule(audit_2, secret=secret_v2, store=store)
        # Must NOT raise — a lawfully-minted pre-rotation token must
        # still verify once the operator mounts the historical key.
        await gates_v2.grant(v1_token)
        await audit_2._writer.flush()
    finally:
        await audit_2.close()

    # Verified = the audit row landed with approval.granted.
    granted_events = [
        e
        for e in audit_store_2._events.values()  # type: ignore[attr-defined]
        if e.kind == "approval.granted"
    ]
    assert len(granted_events) == 1
    assert granted_events[0].metadata["request_id"] == str(req.request_id)


@pytest.mark.asyncio
async def test_historical_key_missing_yields_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-rotation, un-mounted historical key → distinct error code.

    The spec requires: "Missing historical key env var = reject with a
    distinct error code (not a generic 'invalid')." This test mints
    under v1, rotates to v2, does NOT set ``GOVERNANCE_CHAIN_KEY_<v1>``,
    and asserts the rejection uses the ``historical key unavailable``
    wording rather than ``signature mismatch`` — operators running the
    ``cga rotate-chain-key`` runbook rely on this distinction to
    discover they forgot to carry over the old key env var.
    """
    secret_v1 = _fresh_secret()
    secret_v2 = _fresh_secret()

    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    v1_token = make_token(
        secret=secret_v1,
        request_id=uuid4(),
        action_hash="a" * 64,
        expires_at=expires,
    )

    # Ensure no historical env var is mounted.
    env_name = _prefix_env_name(secret_v1)
    monkeypatch.delenv(env_name, raising=False)

    with pytest.raises(
        ApprovalTokenError, match="historical key unavailable"
    ):
        parse_token(secret=secret_v2, token=v1_token)


@pytest.mark.asyncio
async def test_tampered_hmac_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token with a mutated HMAC suffix → ``signature mismatch``.

    When the prefix IS mountable (current secret, or operator set the
    env var), the verifier resolves the key and runs a real HMAC check.
    Flipping a nibble of the trailing mac must land on the signature-
    mismatch branch, not the historical-key-unavailable branch — the
    latter would teach operators the wrong lesson about forged tokens.
    """
    secret = _fresh_secret()
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    good = make_token(
        secret=secret,
        request_id=uuid4(),
        action_hash="a" * 64,
        expires_at=expires,
    )
    # Flip the last hex char.
    last = good[-1]
    flipped = "0" if last != "0" else "1"
    tampered = good[:-1] + flipped

    with pytest.raises(ApprovalTokenError, match="signature mismatch"):
        parse_token(secret=secret, token=tampered)


@pytest.mark.asyncio
async def test_wrong_key_mounted_under_prefix_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: operator mis-wires GOVERNANCE_CHAIN_KEY_<prefix>.

    If the operator accidentally points the env var at a DIFFERENT key
    (say, the audit secret of a different deployment), the verifier
    refuses to verify under that key. Fingerprint mismatch means
    ``resolve`` returns None → distinct ``historical key unavailable``
    error, NOT a false-positive verify under wrong bytes.
    """
    secret_v1 = _fresh_secret()
    secret_v2 = _fresh_secret()
    wrong_secret = _fresh_secret()

    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    v1_token = make_token(
        secret=secret_v1,
        request_id=uuid4(),
        action_hash="a" * 64,
        expires_at=expires,
    )

    # Mis-wire: prefix slot for v1 points at wrong_secret's bytes.
    monkeypatch.setenv(_prefix_env_name(secret_v1), "env://WRONG_SECRET")
    monkeypatch.setenv(
        "WRONG_SECRET",
        base64.b64encode(wrong_secret).decode("ascii"),
    )

    with pytest.raises(
        ApprovalTokenError, match="historical key unavailable"
    ):
        parse_token(secret=secret_v2, token=v1_token)


@pytest.mark.asyncio
async def test_legacy_v1_format_still_accepted_under_current_secret() -> None:
    """Pre-v0.6.2 minted tokens (no ``v2:`` prefix) still verify.

    We can't retroactively bind legacy tokens to a key fingerprint, so
    they verify under the current ``secret`` argument only. This is
    the upgrade-path back-compat: tokens in flight at the moment of
    deploying v0.6.2 must not break.
    """
    import hashlib
    import hmac

    secret = _fresh_secret()
    rid = uuid4()
    action_hash = "a" * 64
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    # Reconstruct the legacy v1 format by hand.
    legacy_body = f"{rid}:{action_hash}:{expires_at.isoformat()}"
    legacy_mac = hmac.new(
        secret, legacy_body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    legacy_token = f"{legacy_body}:{legacy_mac}"

    assert not legacy_token.startswith(f"{TOKEN_VERSION_V2}:")

    parsed_rid, parsed_ah, _ = parse_token(secret=secret, token=legacy_token)
    assert parsed_rid == rid
    assert parsed_ah == action_hash


@pytest.mark.asyncio
async def test_malformed_v2_prefix_rejected() -> None:
    """A v2-flagged token with a malformed prefix is rejected cleanly."""
    rid = uuid4()
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    # Prefix "abc" is not 16 chars — reject as malformed-prefix, not
    # as signature mismatch.
    body = f"{TOKEN_VERSION_V2}:abc:{rid}:aaaaaaaa:{expires.isoformat()}"
    # HMAC irrelevant — we expect parse to bail on the prefix length
    # check BEFORE computing the MAC, but even if it didn't, it would
    # land on signature mismatch. We accept either message.
    token = body + ":" + ("0" * 64)
    with pytest.raises(
        ApprovalTokenError, match="malformed key prefix|signature mismatch"
    ):
        parse_token(secret=_fresh_secret(), token=token)
