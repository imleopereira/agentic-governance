"""Happy-path + exploit tests for the HITL gates module."""
from __future__ import annotations

import asyncio
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
from codeatelier_governance.gates import (
    ApprovalDenied,
    ApprovalPending,
    ApprovalTimeout,
    ApprovalTokenError,
    GatesModule,
)
from codeatelier_governance.gates.tokens import make_token, parse_token


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_request_returns_signed_token(gates: GatesModule) -> None:
    req = await gates.request("delete.user", "agent-1", payload={"user_id": "u1"})
    assert req.kind == "delete.user"
    assert req.agent_id == "agent-1"
    assert req.token.count(":") >= 3
    assert req.expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_grant_resolves_request(
    gates: GatesModule, audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    req = await gates.request("delete.user", "a", payload={})
    await gates.grant(req.token)
    await audit._writer.flush()
    granted_events = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "approval.granted"
    ]
    assert len(granted_events) == 1
    assert granted_events[0].metadata["request_id"] == str(req.request_id)


@pytest.mark.asyncio
async def test_deny_logs_denial(
    gates: GatesModule, audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    req = await gates.request("delete.user", "a", payload={})
    await gates.deny(req.token)
    await audit._writer.flush()
    denied = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "approval.denied"
    ]
    assert len(denied) == 1


@pytest.mark.asyncio
async def test_wait_for_resolves_after_grant(gates: GatesModule) -> None:
    req = await gates.request("k", "a", payload={})

    async def grant_later() -> None:
        # Brief delay to simulate async human approval arriving after wait_for starts
        await asyncio.sleep(0.05)
        await gates.grant(req.token)

    asyncio.create_task(grant_later())
    granted = await gates.wait_for(req.request_id, timeout=2.0)
    assert granted is True


@pytest.mark.asyncio
async def test_wait_for_raises_on_deny(gates: GatesModule) -> None:
    req = await gates.request("k", "a", payload={})

    async def deny_later() -> None:
        # Brief delay to simulate async human denial arriving after wait_for starts
        await asyncio.sleep(0.05)
        await gates.deny(req.token)

    asyncio.create_task(deny_later())
    with pytest.raises(ApprovalDenied):
        await gates.wait_for(req.request_id, timeout=2.0)


@pytest.mark.asyncio
async def test_wait_for_times_out(gates: GatesModule) -> None:
    req = await gates.request("k", "a", payload={})
    with pytest.raises(ApprovalTimeout):
        await gates.wait_for(req.request_id, timeout=0.1)


@pytest.mark.asyncio
async def test_blocking_decorator_runs_after_grant(gates: GatesModule) -> None:
    # The decorator opens the request synchronously inside the wrapper, so we
    # use the public ``request`` API to drive a parallel scenario instead.
    @gates.require_approval(kind="charge", agent_id="a", timeout=3.0)
    async def charge(amount: int) -> str:
        return f"charged {amount}"

    # Walk the in-memory store after the decorator opens the request.
    async def approve_eventually() -> None:
        # Brief delay so the decorator's wait_for loop starts before we grant
        await asyncio.sleep(0.1)
        store = gates._store  # type: ignore[attr-defined]
        # InMemoryGatesStore exposes _pending dict for tests
        async with store._lock:  # type: ignore[attr-defined]
            req = next(iter(store._pending.values()))  # type: ignore[attr-defined]
        await gates.grant(req.token)

    asyncio.create_task(approve_eventually())
    result = await charge(100)
    assert result == "charged 100"


@pytest.mark.asyncio
async def test_non_blocking_decorator_raises_pending(gates: GatesModule) -> None:
    @gates.require_approval(
        kind="charge", agent_id="a", blocking=False, timeout=1.0
    )
    async def charge(amount: int) -> str:
        return f"charged {amount}"

    with pytest.raises(ApprovalPending):
        await charge(100)


# ---------------------------------------------------------------------------
# Exploit / cybersecurity tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_short_secret_is_rejected(audit) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="at least"):
        GatesModule(audit, secret=b"too-short")


@pytest.mark.asyncio
async def test_forged_token_rejected(gates: GatesModule) -> None:
    """A token signed by a DIFFERENT secret must fail verification.

    v0.6.2: the v2 format embeds the signing key's fingerprint prefix
    in the token body. A forged token signed by a random secret will
    carry a random prefix that neither matches the current secret nor
    any historical key mounted via ``GOVERNANCE_CHAIN_KEY_<prefix>``,
    so verification now rejects it with
    ``"historical key unavailable"`` rather than ``"signature
    mismatch"``. Either message is an ``ApprovalTokenError`` and the
    operational answer is the same (reject); the test asserts only the
    rejection, not the specific error string. See
    ``tests/gates/test_token_rotation.py::test_tampered_hmac_rejected``
    for the dedicated signature-mismatch assertion.
    """
    req = await gates.request("k", "a", payload={})
    fake_secret = _secrets.token_bytes(32)
    forged = make_token(
        secret=fake_secret,
        request_id=req.request_id,
        action_hash=req.action_hash,
        expires_at=req.expires_at,
    )
    with pytest.raises(ApprovalTokenError):
        await gates.grant(forged)


@pytest.mark.asyncio
async def test_token_replay_rejected(gates: GatesModule) -> None:
    """A token can only be used once."""
    req = await gates.request("k", "a", payload={})
    await gates.grant(req.token)
    with pytest.raises(ApprovalTokenError, match="already used"):
        await gates.grant(req.token)


@pytest.mark.asyncio
async def test_token_with_wrong_action_hash_rejected(
    gates: GatesModule, secret: bytes
) -> None:
    """An attacker who tries to swap the action_hash invalidates the token."""
    req = await gates.request("k", "a", payload={"original": True})
    # Forge a token with the SAME request_id but a DIFFERENT action_hash
    bad_token = make_token(
        secret=secret,
        request_id=req.request_id,
        action_hash="0" * 64,  # not the real action_hash
        expires_at=req.expires_at,
    )
    with pytest.raises(ApprovalTokenError, match="action_hash mismatch"):
        await gates.grant(bad_token)


@pytest.mark.asyncio
async def test_expired_token_rejected(gates: GatesModule, secret: bytes) -> None:
    expired = make_token(
        secret=secret,
        request_id=uuid4(),
        action_hash="a" * 64,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(ApprovalTokenError, match="expired"):
        parse_token(secret=secret, token=expired)


@pytest.mark.asyncio
async def test_unknown_request_id_rejected(gates: GatesModule, secret: bytes) -> None:
    """Token with valid signature but request_id not in pending must fail."""
    fake = make_token(
        secret=secret,
        request_id=uuid4(),
        action_hash="b" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    with pytest.raises(ApprovalTokenError, match="unknown request_id"):
        await gates.grant(fake)


@pytest.mark.asyncio
async def test_decorator_rejects_sync_function(gates: GatesModule) -> None:
    with pytest.raises(TypeError, match="async function"):

        @gates.require_approval(kind="x", agent_id="a")
        def sync_fn() -> None:
            pass


@pytest.mark.asyncio
async def test_concurrent_grant_and_deny_only_first_wins(
    gates: GatesModule,
) -> None:
    """If grant and deny race, exactly one succeeds; the other raises."""
    req = await gates.request("k", "a", payload={})
    results = await asyncio.gather(
        gates.grant(req.token),
        gates.deny(req.token),
        return_exceptions=True,
    )
    successes = [r for r in results if r is None]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ApprovalTokenError)
