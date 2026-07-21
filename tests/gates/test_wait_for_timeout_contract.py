"""Contract tests for wait_for timeout behaviour.

These tests pin the current (intentional) behaviour when wait_for exhausts
its deadline without an operator decision:

1. ApprovalTimeout is raised — the caller is unblocked immediately.
2. No reverse-sync POST is fired to the platform — the gate stays "pending"
   on the platform so an operator can still see and act on it after the SDK
   gave up. This is a deliberate security invariant (see the TODO comment in
   gates/module.py:wait_for): the agent process must not write a terminal
   resolution to the platform on its own authority.
3. The gate remains in _forwarded_request_ids after timeout — the
   infrastructure for a future notification is in place.
4. Without a platform client, ApprovalTimeout is raised without errors.
5. Timeout fires even when the platform consistently returns "pending".

Context: a v0.7.2 team review (DA + Security) identified three blockers that
prevent shipping a "notify platform on timeout" implementation safely. Until
those are resolved, this suite enforces the current no-notification contract
so any accidental change is caught immediately.
"""
from __future__ import annotations

import asyncio
import secrets
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from codeatelier_governance.audit.module import AuditModule
from codeatelier_governance.audit.store import InMemoryAuditStore
from codeatelier_governance.gates import ApprovalTimeout, GatesModule
from codeatelier_governance.platform.client import PlatformClient
from codeatelier_governance.platform.config import PlatformConfig


_BASE_URL = "https://platform.timeout-contract.test"
_INGEST_URL = f"{_BASE_URL}/api/v1/ingest/events"
_TEST_TOKEN = "wf_gate_timeout_contract_test"  # pragma: allowlist secret
_SECRET = secrets.token_bytes(32)


def _make_config() -> PlatformConfig:
    return PlatformConfig(
        ingest_url=_INGEST_URL,
        ingest_token=_TEST_TOKEN,
        enabled=True,
        timeout_seconds=2.0,
    )


async def _make_gates(
    platform_client: PlatformClient | None = None,
) -> tuple[GatesModule, AuditModule, InMemoryAuditStore]:
    audit_store = InMemoryAuditStore()
    audit = AuditModule(store=audit_store, secret=_SECRET)
    await audit.start()
    gates = GatesModule(
        audit,
        secret=_SECRET,
        platform_client=platform_client,
        poll_interval_s=0.01,
    )
    return gates, audit, audit_store


def _resolution_url(request_id: UUID) -> str:
    return f"{_BASE_URL}/api/v1/bridge/gates/{request_id}/resolution"


def _forward_url(request_id: UUID) -> str:
    return f"{_BASE_URL}/api/v1/bridge/gates/{request_id}"


# ---------------------------------------------------------------------------
# 1. ApprovalTimeout raised cleanly with platform client wired
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_raises_approval_timeout_with_platform_wired() -> None:
    """wait_for must raise ApprovalTimeout when the deadline passes,
    even with a live platform client forwarding the gate.
    """
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").respond(
            200, json={"status": "pending", "decided_at": None}
        )
        client = PlatformClient(_make_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request("email.send", "notifier-agent", payload={})
            # Drain the forward so _forwarded_request_ids is populated.
            for _ in range(100):
                await asyncio.sleep(0.01)
                if client.stats()["gate_requests_sent"] >= 1:
                    break

            with pytest.raises(ApprovalTimeout):
                await gates.wait_for(req.request_id, timeout=0.1)
        finally:
            await client.close()
            await gates.close()


# ---------------------------------------------------------------------------
# 2. No reverse-sync POST on timeout — platform gate stays "pending"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_does_not_post_resolution_to_platform() -> None:
    """The SDK must NOT fire a reverse-sync POST when wait_for times out.

    Security invariant: the agent process must not write a terminal gate
    resolution. Doing so would let a compromised agent force a "handled"
    state on the platform while still executing the blocked action.
    See the TODO comment in gates/module.py for the full blocker list.
    """
    resolution_posts: list[Any] = []

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").respond(
            200, json={"status": "pending", "decided_at": None}
        )
        # Any POST to the /resolution sub-path is a violation.
        mock.post(url__regex=r".*/bridge/gates/.*/resolution$").mock(
            side_effect=lambda r: resolution_posts.append(r) or httpx.Response(
                200, json={"ok": True}
            )
        )

        client = PlatformClient(_make_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request("email.send", "notifier-agent", payload={})
            for _ in range(100):
                await asyncio.sleep(0.01)
                if client.stats()["gate_requests_sent"] >= 1:
                    break

            with pytest.raises(ApprovalTimeout):
                await gates.wait_for(req.request_id, timeout=0.1)

            # Give any fire-and-forget tasks time to flush.
            await asyncio.sleep(0.1)
        finally:
            await client.close()
            await gates.close()

    assert resolution_posts == [], (
        f"SDK must not POST a resolution on timeout, but fired "
        f"{len(resolution_posts)} POST(s) to /resolution"
    )
    # local_resolutions_* stats must stay zero.
    stats = client.stats()
    assert stats["local_resolutions_sent"] == 0
    assert stats["local_resolutions_dropped"] == 0


# ---------------------------------------------------------------------------
# 3. Gate remains in _forwarded_request_ids after timeout
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_gate_remains_forwarded() -> None:
    """After timeout the request_id must still be in _forwarded_request_ids.

    This preserves the ability for a future (properly reviewed) implementation
    to notify the platform retroactively without rebuilding the forwarded-set
    tracking infrastructure.
    """
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").respond(
            200, json={"status": "pending", "decided_at": None}
        )
        client = PlatformClient(_make_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request("email.send", "notifier-agent", payload={})
            for _ in range(100):
                await asyncio.sleep(0.01)
                if client.stats()["gate_requests_sent"] >= 1:
                    break

            with pytest.raises(ApprovalTimeout):
                await gates.wait_for(req.request_id, timeout=0.1)

            assert req.request_id in client._forwarded_request_ids  # type: ignore[attr-defined]
        finally:
            await client.close()
            await gates.close()


# ---------------------------------------------------------------------------
# 4. ApprovalTimeout raised cleanly without a platform client
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_no_platform_client_raises_cleanly() -> None:
    """wait_for must raise ApprovalTimeout without any errors when no
    platform client is wired (pure-local deployment).
    """
    gates, _audit, _ = await _make_gates(platform_client=None)
    try:
        req = await gates.request("email.send", "notifier-agent", payload={})
        with pytest.raises(ApprovalTimeout):
            await gates.wait_for(req.request_id, timeout=0.05)
    finally:
        await gates.close()


# ---------------------------------------------------------------------------
# 5. Platform gate remains "pending" after SDK timeout (from platform's view)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_platform_gate_still_pending_after_sdk_timeout() -> None:
    """From the platform's perspective the gate must remain "pending" after
    the SDK times out. No write should have changed its state.

    This verifies that a human operator could still approve or deny the gate
    after the SDK caller received ApprovalTimeout — the oversight window is
    not closed by the SDK giving up.
    """
    get_calls: list[str] = []

    def _resolution_handler(request: httpx.Request) -> httpx.Response:
        get_calls.append(str(request.url))
        return httpx.Response(200, json={"status": "pending", "decided_at": None})

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").mock(
            side_effect=_resolution_handler
        )

        client = PlatformClient(_make_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request("email.send", "notifier-agent", payload={})
            for _ in range(100):
                await asyncio.sleep(0.01)
                if client.stats()["gate_requests_sent"] >= 1:
                    break

            with pytest.raises(ApprovalTimeout):
                await gates.wait_for(req.request_id, timeout=0.1)

            # The last platform response was "pending" — no write occurred
            # that could have changed it. Every GET in get_calls returned
            # "pending" and no POST to /resolution was ever made.
            assert len(get_calls) >= 1, "expected at least one platform poll"
            assert client.stats()["local_resolutions_sent"] == 0
        finally:
            await client.close()
            await gates.close()
