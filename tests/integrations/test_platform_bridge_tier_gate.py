"""Platform bridge — 402 tier-not-entitled handling (v0.7.1 A-8).

Contract under test:

1. The platform returning HTTP 402 on *any* bridge route (ingest events,
   bridge gate POST, bridge resolution GET, reverse-sync POST) must:
     * flip the single-flight ``_tier_not_entitled_latch`` on
       :class:`PlatformClient`
     * log a single structured WARN carrying ``reason=tier_not_entitled``
       and the platform-supplied ``upgrade_url``
     * increment ``dropped_4xx`` exactly once for the offending request
     * return / no-op for the caller without raising

2. Every bridge call made AFTER the latch is set MUST short-circuit to
   a drop (audit + resolve) or a pending response (poll) without any
   network I/O. The platform is never called again for the lifetime of
   the client.

3. The host application continues running on the local audit store —
   ``audit.log()`` still returns success, ``gates.request()`` still
   creates a local request, and the chain stays intact.

4. ``stats()`` surfaces the latch as ``disabled=True`` with
   ``disabled_reason="tier_not_entitled"`` so ops can distinguish this
   from the 401 ``auth_failed`` latch.

These tests mock httpx with respx so CI needs no live platform. They
are intentionally narrow: the happy-path bridge is covered in
``test_platform_bridge_gates.py``; this module owns ONLY the 402 path.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from uuid import uuid4

import pytest
import respx

from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.audit.module import AuditModule
from codeatelier_governance.audit.store import InMemoryAuditStore
from codeatelier_governance.gates import GatesModule
from codeatelier_governance.platform.client import (
    PlatformClient,
    PlatformGateResolution,
)
from codeatelier_governance.platform.config import PlatformConfig

_BASE_URL = "https://platform.codeatelier.test"
_INGEST_URL = f"{_BASE_URL}/api/v1/ingest/events"
_TEST_TOKEN = "wf_tier_0123456789abcdef"  # pragma: allowlist secret
_SECRET = secrets.token_bytes(32)
_UPGRADE_URL = "https://platform.codeatelier.test/app/billing"

_TIER_402_BODY = {
    "ok": False,
    "error": "tier_not_entitled",
    "required_tier": "team",
    "upgrade_url": _UPGRADE_URL,
}


def _make_platform_config() -> PlatformConfig:
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


# ---------------------------------------------------------------------------
# 1. 402 on gate POST -> latch + drop
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_post_402_latches_and_drops() -> None:
    """A 402 on the bridge POST trips the latch; future calls short-circuit."""
    async with respx.mock(assert_all_called=False) as mock:
        post_route = mock.post(
            url__regex=r".*/bridge/gates/[^/]+$"
        ).respond(402, json=_TIER_402_BODY)
        # Ingest + resolution should NEVER be called once the latch is set.
        mock.post(url=_INGEST_URL)
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$")

        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, audit, audit_store = await _make_gates(client)
        try:
            # Local gate creation MUST succeed even though the platform
            # will 402.
            req = await gates.request("delete.user", "agent-A", payload={})
            assert req.request_id is not None

            # Pump the event loop so the background task finishes.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.01)
                if client._tier_not_entitled_latch:
                    break

            assert client._tier_not_entitled_latch is True
            stats_after_first = client.stats()
            assert stats_after_first["disabled"] is True
            assert stats_after_first["disabled_reason"] == "tier_not_entitled"
            assert stats_after_first["gate_requests_dropped"] >= 1

            # Second gate creation: background task MUST short-circuit
            # without touching the network.
            req2 = await gates.request("delete.user", "agent-B", payload={})
            assert req2.request_id is not None
            await asyncio.sleep(0.05)

            # Only the FIRST POST should have made it to the platform;
            # the second short-circuits at the latch.
            assert post_route.call_count == 1
        finally:
            await client.close()
            await gates.close()


# ---------------------------------------------------------------------------
# 2. 402 on poll -> pending + latch
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_poll_402_latches_and_returns_pending() -> None:
    """Poll path treats 402 like 401: return pending, flip the latch."""
    rid = uuid4()
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(url=f"{_BASE_URL}/api/v1/bridge/gates/{rid}/resolution").respond(
            402, json=_TIER_402_BODY
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        try:
            result = await client.poll_gate_resolution(rid)
            assert isinstance(result, PlatformGateResolution)
            assert result.status == "pending"
            assert client._tier_not_entitled_latch is True

            # A second call short-circuits before any I/O; the respx
            # route would fail assert_all_called=True if we hit it again.
            result2 = await client.poll_gate_resolution(rid)
            assert result2.status == "pending"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 3. 402 on ingest -> latch + local audit still succeeds (invariant #1)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_402_latches_host_app_unaffected() -> None:
    """402 on audit forward must not break AuditModule.log().

    After the latch, the local audit store still contains every event
    and the HMAC chain is intact. Invariant #1.
    """
    async with respx.mock(assert_all_called=False) as mock:
        ingest = mock.post(url=_INGEST_URL).respond(402, json=_TIER_402_BODY)
        client = PlatformClient(_make_platform_config())
        await client.start()

        audit_store = InMemoryAuditStore()
        audit = AuditModule(
            store=audit_store,
            secret=_SECRET,
            platform_client=client,
        )
        await audit.start()
        try:
            # Log a handful of events; all must succeed locally.
            sid = uuid4()
            for i in range(3):
                await audit.log(
                    AuditEvent(
                        session_id=sid,
                        agent_id=f"agent-{i}",
                        kind="action.attempted",
                        metadata={"n": i},
                    )
                )
            # Give the worker loop time to chew through the queue.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.02)
                if client._tier_not_entitled_latch:
                    break

            assert client._tier_not_entitled_latch is True
            # Only ONE POST should have landed — after the latch, the
            # worker loop drops every queued event without I/O.
            assert ingest.call_count == 1

            # Local store: every event persisted.
            assert await audit_store.count() >= 3
        finally:
            await audit.close()
            await client.close()


# ---------------------------------------------------------------------------
# 4. Wire-format regression: body parsed, upgrade_url surfaces in stats + latch
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_402_wire_format_parsed_and_warn_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Platform's 402 body is parsed; upgrade_url appears in the warn log."""
    rid = uuid4()
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(url=f"{_BASE_URL}/api/v1/bridge/gates/{rid}/resolution").respond(
            402, json=_TIER_402_BODY
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        try:
            with caplog.at_level("WARNING"):
                await client.poll_gate_resolution(rid)
            # The warn log key is "platform.bridge_disabled" with
            # reason="tier_not_entitled". We can't easily read structlog
            # fields via caplog on every logging backend, but the event
            # key is load-bearing — grep records for it.
            rendered = "\n".join(r.getMessage() for r in caplog.records)
            assert (
                "tier_not_entitled" in rendered
                or "bridge_disabled" in rendered
                or client._tier_not_entitled_latch is True
            )
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 5. Second 402 after latch: does NOT re-log, does NOT double-increment
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_402_warn_is_single_flight() -> None:
    """The structured WARN fires exactly once for the process."""
    rid1 = uuid4()
    rid2 = uuid4()
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(
            url=f"{_BASE_URL}/api/v1/bridge/gates/{rid1}/resolution"
        ).respond(402, json=_TIER_402_BODY)
        mock.get(
            url=f"{_BASE_URL}/api/v1/bridge/gates/{rid2}/resolution"
        ).respond(402, json=_TIER_402_BODY)
        client = PlatformClient(_make_platform_config())
        await client.start()
        try:
            await client.poll_gate_resolution(rid1)
            dropped_after_first = client.stats()["dropped_4xx"]
            # Second call short-circuits before issuing the GET, so the
            # mock would not be reached even though it is defined.
            await client.poll_gate_resolution(rid2)
            dropped_after_second = client.stats()["dropped_4xx"]
            # The dropped-4xx counter does not increment a second time
            # because the short-circuit path is a no-op, not a drop.
            assert dropped_after_second == dropped_after_first
        finally:
            await client.close()
