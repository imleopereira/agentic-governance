"""v0.6.2-followup — v2 token minting must be opt-in (downgrade-safe).

Rolling-deploy scenario: operator runs mixed v0.6.1 + v0.6.2 pods
behind a load balancer. A v0.6.2 pod mints a grant token and hands it
to a human reviewer. The human clicks approve and the request lands
on a v0.6.1 pod — which does not understand the ``v2:<hex>:`` prefix
and rejects the token as ``bad request_id`` (its v1 parser tries to
UUID-parse the literal string ``"v2"``).

v0.6.2 SHOULD NOT brick approvals on old pods during the rolling
window. v0.6.2 therefore defaults ``enable_v2_tokens=False`` (mints v1
for back-compat). v2 verification still works so anyone who already
minted v2 tokens in this session (e.g. on a canary pod) continues to
verify them. The flip-to-v2-by-default moves to v0.6.3 once operators
expect all pods to be ≥v0.6.2.

Also asserts that the constructor arg wins over the env var, and that
the env var is honored per the ``.agents/swe.md`` rule on read-at-call-
time env var access.
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
async def test_default_mints_v1_format_for_downgrade_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no flag and no env var, v0.6.2 mints v1 tokens."""
    monkeypatch.delenv("GOVERNANCE_GATES_ENABLE_V2_TOKENS", raising=False)
    secret = _secrets.token_bytes(32)
    audit = await _build_audit(secret)
    try:
        gates = GatesModule(audit, secret=secret)
        req = await gates.request("delete.patient", "agent-alpha", payload={})
    finally:
        await audit.close()

    # v1 tokens do NOT start with ``v2:``. A v0.6.1 parser would split
    # on ":" and treat parts[0] as the request_id UUID — which it is.
    assert not req.token.startswith(f"{TOKEN_VERSION_V2}:"), (
        "Default GatesModule minted a v2 token — breaks rolling "
        "deploys with v0.6.1 pods."
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
