"""v2 tokens are the default; v1 minting stays available for downgrades.

The v2 (``v2:<key_prefix>:``) token format is rotation-aware and is what
``GatesModule`` mints by default. Minting v1 was once the default so a
mixed rolling deploy (a v0.6.1 pod handling grant/deny for a token minted
by a v0.6.2 pod) would not brick approvals, since the v0.6.1 parser
cannot read a ``v2:`` prefix. Those windows have long closed, so v2 is
now the default. Operators who still need the old behavior can opt back
into v1 minting via ``enable_v2_tokens=False`` or
``GOVERNANCE_GATES_ENABLE_V2_TOKENS=false``; v2 tokens verify regardless
of the mint preference.

These tests also assert that the constructor arg wins over the env var,
and that the env var is honored (read at call time).
"""
from __future__ import annotations

import secrets as _secrets

import pytest

from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
from codeatelier_governance.audit.store import BatchingWriter
from codeatelier_governance.gates import GatesModule
from codeatelier_governance.gates.tokens import TOKEN_VERSION_V2, parse_token


async def _build_audit(secret: bytes) -> AuditModule:
    store = InMemoryAuditStore(max_events=10_000)
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02)
    audit = AuditModule(store, secret=secret, writer=writer)
    await audit.start()
    return audit


@pytest.mark.asyncio
async def test_default_mints_v2_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no flag and no env var, the default mints v2 tokens."""
    monkeypatch.delenv("GOVERNANCE_GATES_ENABLE_V2_TOKENS", raising=False)
    secret = _secrets.token_bytes(32)
    audit = await _build_audit(secret)
    try:
        gates = GatesModule(audit, secret=secret)
        req = await gates.request("delete.patient", "agent-alpha", payload={})
    finally:
        await audit.close()

    assert req.token.startswith(f"{TOKEN_VERSION_V2}:"), (
        "Default GatesModule should mint a v2 (rotation-aware) token."
    )
    # Round-trip verifies under the current secret.
    parsed_rid, _, _ = parse_token(secret=secret, token=req.token)
    assert parsed_rid == req.request_id


@pytest.mark.asyncio
async def test_opt_in_flag_mints_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enable_v2_tokens=True mints v2 (rotation-aware) tokens."""
    monkeypatch.delenv("GOVERNANCE_GATES_ENABLE_V2_TOKENS", raising=False)
    secret = _secrets.token_bytes(32)
    audit = await _build_audit(secret)
    try:
        gates = GatesModule(audit, secret=secret, enable_v2_tokens=True)
        req = await gates.request("delete.patient", "agent-alpha", payload={})
    finally:
        await audit.close()

    assert req.token.startswith(f"{TOKEN_VERSION_V2}:")
    parsed_rid, _, _ = parse_token(secret=secret, token=req.token)
    assert parsed_rid == req.request_id


@pytest.mark.asyncio
async def test_env_var_enables_v2_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GOVERNANCE_GATES_ENABLE_V2_TOKENS=true opts in without code changes."""
    monkeypatch.setenv("GOVERNANCE_GATES_ENABLE_V2_TOKENS", "true")
    secret = _secrets.token_bytes(32)
    audit = await _build_audit(secret)
    try:
        gates = GatesModule(audit, secret=secret)
        req = await gates.request("delete.patient", "agent-alpha", payload={})
    finally:
        await audit.close()
    assert req.token.startswith(f"{TOKEN_VERSION_V2}:")


@pytest.mark.asyncio
async def test_constructor_arg_wins_over_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit False kwarg beats an env var that would have enabled v2."""
    monkeypatch.setenv("GOVERNANCE_GATES_ENABLE_V2_TOKENS", "true")
    secret = _secrets.token_bytes(32)
    audit = await _build_audit(secret)
    try:
        gates = GatesModule(audit, secret=secret, enable_v2_tokens=False)
        req = await gates.request("delete.patient", "agent-alpha", payload={})
    finally:
        await audit.close()
    assert not req.token.startswith(f"{TOKEN_VERSION_V2}:")


@pytest.mark.asyncio
async def test_v2_verification_still_works_with_v1_minting() -> None:
    """Verification is back-compat: even with enable_v2_tokens=False, a v2
    token (say, minted by a canary pod that flipped to True) still
    verifies correctly through the grant path.
    """
    secret = _secrets.token_bytes(32)
    audit_mint = await _build_audit(secret)
    try:
        # Pod A mints v2.
        gates_v2_mint = GatesModule(
            audit_mint, secret=secret, enable_v2_tokens=True
        )
        req = await gates_v2_mint.request(
            "delete.patient", "agent-alpha", payload={}
        )
    finally:
        await audit_mint.close()
    assert req.token.startswith(f"{TOKEN_VERSION_V2}:")

    # Pod B (v1-minting default) grants the v2 token. SHARED store
    # so the pending row survives the module rebuild.
    audit_grant = await _build_audit(secret)
    try:
        gates_v1_mint = GatesModule(
            audit_grant,
            secret=secret,
            store=gates_v2_mint._store,  # type: ignore[attr-defined]
            enable_v2_tokens=False,
        )
        # Must NOT raise — v2 parse path is identical regardless of
        # mint preference.
        await gates_v1_mint.grant(req.token)
    finally:
        await audit_grant.close()
