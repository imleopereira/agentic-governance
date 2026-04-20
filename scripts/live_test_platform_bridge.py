#!/usr/bin/env python3
"""Live test: SDK v0.7.1 platform-bridge HITL gates against real Next + Postgres.

Runs the v0.7.1 pass-2 real-usage verification for the SDK <-> platform
bridge. Exits 1 with remediation instructions if the local dev stack is
not running.

Prerequisites:
    * Code Atelier Platform running locally at http://localhost:3100
      (cd /Users/leopereira/code/claude-teams/code-atelier-platform
       && npm run dev -- --port 3100)
    * Local Supabase running with DB at port 54432
      (cd /Users/leopereira/code/claude-teams/code-atelier-platform
       && supabase start)
    * Migrations applied (0001-0010) including the amended 0008 with
      by_source column.
    * Seed tenant + workflow rows present (deterministic fixture IDs).

Three flows covered (per the pass-2 brief):
    1. Happy path: create gate, poll says pending, admin decides via
       direct SQL call to resolve_gate RPC, SDK's wait_for returns
       granted, audit has by=platform.
    2. Local wins: create gate, grant locally, platform card transitions
       to by_source=sdk_local via the new reverse-sync POST.
    3. Platform down: point at a dead port -> SDK still resolves via
       local grant, gate_polls_failed > 0.

The script bypasses PostgREST (which has cache/auth quirks in a reset
DB) and talks directly to Postgres via asyncpg — mirrors the pattern
in scripts/live_test.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx


# --- Colors ---------------------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"


def _log(tag: str, msg: str, color: str = CYAN) -> None:
    print(f"  {color}[{tag}]{RESET} {msg}", flush=True)


# --- Config ---------------------------------------------------------------
PLATFORM_BASE_URL = os.environ.get(
    "PLATFORM_BASE_URL", "http://localhost:3100"
)
# Local Supabase Postgres port for the platform project.
PG_DSN = os.environ.get(
    "PLATFORM_DB_DSN",
    "postgres://postgres:postgres@127.0.0.1:54432/postgres",
)
SEED_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SEED_WORKFLOW_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
SEED_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


# --- Preflight ------------------------------------------------------------
def preflight() -> None:
    """Verify local stack is reachable; exit 1 with actionable error otherwise."""
    _log("preflight", f"checking platform at {PLATFORM_BASE_URL}...")
    try:
        r = httpx.get(f"{PLATFORM_BASE_URL}/", timeout=3.0)
        if r.status_code >= 500:
            raise RuntimeError(f"platform returned {r.status_code}")
    except Exception as exc:
        sys.stderr.write(
            f"{RED}FATAL{RESET} Platform not reachable at {PLATFORM_BASE_URL}:"
            f" {exc}\n"
            f"  Start it with:\n"
            f"    cd /Users/leopereira/code/claude-teams/code-atelier-platform\n"
            f"    npm run dev -- --port 3100\n"
        )
        sys.exit(1)
    _log("preflight", f"{GREEN}OK{RESET} platform reachable")


async def _connect_pg() -> Any:
    """Open a direct asyncpg connection to the platform DB, retrying.

    Local supabase cycles frequently during extensive dev sessions;
    we retry up to 15s with 1s backoff so a transient restart doesn't
    fail the script.
    """
    import asyncpg  # deferred import — only needed for live-test

    last_exc: Exception | None = None
    for _ in range(15):
        try:
            return await asyncpg.connect(dsn=PG_DSN)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            await asyncio.sleep(1.0)
    raise RuntimeError(
        f"could not connect to Postgres after 15s: {last_exc}"
    )


async def _connect_admin_pg() -> Any:
    """Open a direct asyncpg connection as supabase_admin (can GRANT)."""
    import asyncpg

    admin_dsn = PG_DSN.replace("postgres://postgres:", "postgres://supabase_admin:")
    last_exc: Exception | None = None
    for _ in range(15):
        try:
            return await asyncpg.connect(dsn=admin_dsn)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            await asyncio.sleep(1.0)
    raise RuntimeError(
        f"could not connect to Postgres as supabase_admin: {last_exc}"
    )


async def ensure_auth_user() -> None:
    """Insert a deterministic auth.users row for resolve_gate to reference.

    Only needed for flow 1 which simulates the admin-decides path. The
    resolve_gate RPC requires ``decided_by`` to foreign-key to
    auth.users(id). We use ``SET ROLE supabase_auth_admin`` to get the
    required privileges (postgres role is denied by default).
    """
    conn = await _connect_admin_pg()
    try:
        # Idempotent insert. crypt() comes from pgcrypto.
        await conn.execute(
            """
            INSERT INTO auth.users (
              instance_id, id, aud, role, email,
              encrypted_password, email_confirmed_at,
              raw_app_meta_data, raw_user_meta_data,
              created_at, updated_at, is_sso_user, is_anonymous
            ) VALUES (
              '00000000-0000-0000-0000-000000000000'::uuid,
              $1::uuid,
              'authenticated', 'authenticated',
              'livetest@acme.test',
              crypt('password123', gen_salt('bf')),
              now(),
              '{"provider":"email","providers":["email"]}'::jsonb,
              '{}'::jsonb,
              now(), now(), false, false
            )
            ON CONFLICT (id) DO NOTHING
            """,
            SEED_USER_ID,
        )
    finally:
        await conn.close()


async def ensure_service_role_grants() -> None:
    """Grant the postgres role + service_role privileges needed for this test.

    Local-dev only. In production the ingest and bridge writes always go
    through the SECURITY DEFINER RPCs (bootstrap_workflow,
    rotate_ingest_token, resolve_gate, bridge_upsert_gate) which bypass
    RLS at the RPC boundary. Our test wants to insert an ingest_tokens
    row directly without minting a user session, so we need explicit
    grants.
    """
    conn = await _connect_admin_pg()
    try:
        for tbl in (
            "tenants",
            "workflows",
            "ingest_tokens",
            "gate_requests",
            "gate_decisions",
        ):
            await conn.execute(
                f"GRANT SELECT, INSERT, UPDATE ON public.{tbl} TO postgres, service_role"
            )
    finally:
        await conn.close()


MIGRATIONS_DIR = (
    "/Users/leopereira/code/claude-teams/code-atelier-platform/supabase/migrations"
)


async def _apply_migrations_if_needed(conn: Any) -> None:
    """If gate_decisions.by_source is missing, re-apply migrations 0001-0010.

    The local supabase DB occasionally cycles through an init sequence
    that only runs the docker-entrypoint migrations (not the project's
    supabase/migrations/*). Rather than fail the script, we silently
    re-apply the amended 0008 (and any earlier migrations that aren't
    idempotent) so the live test can keep running.
    """
    import os as _os

    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='gate_decisions' "
        "AND column_name='by_source'"
    )
    if rows:
        return
    _log(
        "preflight-db",
        "by_source missing — re-applying migrations 0001-0010...",
    )
    files = sorted(
        p for p in _os.listdir(MIGRATIONS_DIR) if p.endswith(".sql")
    )
    # Run each file via psql inside the container.
    import subprocess

    for f in files:
        path = f"{MIGRATIONS_DIR}/{f}"
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "supabase_db_code-atelier-platform",
                "psql",
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=0",
            ],
            stdin=open(path, "rb"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    # Verify.
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='gate_decisions' "
        "AND column_name='by_source'"
    )
    if not rows:
        sys.stderr.write(
            f"{RED}FATAL{RESET} by_source column still missing after "
            f"re-apply. Run supabase db reset manually.\n"
        )
        sys.exit(1)


async def preflight_db() -> None:
    """Confirm migrations applied and seed rows exist."""
    _log("preflight-db", f"connecting to {PG_DSN.split('@')[-1]}...")
    try:
        conn = await _connect_pg()
    except Exception as exc:
        sys.stderr.write(
            f"{RED}FATAL{RESET} Cannot connect to Postgres ({PG_DSN.split('@')[-1]}):"
            f" {exc}\n"
            f"  Start it with:\n"
            f"    cd /Users/leopereira/code/claude-teams/code-atelier-platform\n"
            f"    supabase start\n"
        )
        sys.exit(1)
    try:
        await _apply_migrations_if_needed(conn)
        tcount = await conn.fetchval(
            "SELECT count(*) FROM public.tenants WHERE id=$1", SEED_TENANT_ID
        )
        wcount = await conn.fetchval(
            "SELECT count(*) FROM public.workflows WHERE id=$1",
            SEED_WORKFLOW_ID,
        )
        if not tcount:
            _log("preflight-db", "tenant row missing; inserting seed...")
            await conn.execute(
                "INSERT INTO public.tenants (id, name, slug, tier) "
                "VALUES ($1, 'Acme', 'acme-seed', 'pro') "
                "ON CONFLICT (id) DO NOTHING",
                SEED_TENANT_ID,
            )
        if not wcount:
            _log("preflight-db", "workflow row missing; inserting seed...")
            vault_ref = f"workflow/{SEED_WORKFLOW_ID}/chain_key"
            await conn.execute(
                "INSERT INTO public.workflows "
                "(id, tenant_id, name, description, chain_key_vault_ref) "
                "VALUES ($1, $2, 'acme-primary', 'seed workflow', $3) "
                "ON CONFLICT (id) DO NOTHING",
                SEED_WORKFLOW_ID,
                SEED_TENANT_ID,
                vault_ref,
            )
        _log(
            "preflight-db",
            f"{GREEN}OK{RESET} by_source column + seed rows present",
        )
    finally:
        await conn.close()


# --- Seeding --------------------------------------------------------------
async def seed_ingest_token() -> str:
    """Insert an ingest_tokens row via direct SQL. Returns plaintext bearer."""
    plaintext = f"wf_livetest_{secrets.token_hex(16)}"
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    conn = await _connect_pg()
    try:
        # service_role-owned tables — direct insert works as postgres
        # superuser path. In local dev, postgres is the bootstrap role
        # with sufficient grants. For production we'd go through
        # rotate_ingest_token RPC with authenticated session.
        await conn.execute(
            "INSERT INTO public.ingest_tokens (workflow_id, token_hash, active) "
            "VALUES ($1, $2, true)",
            SEED_WORKFLOW_ID,
            token_hash,
        )
    finally:
        await conn.close()
    _log(
        "seed",
        f"{GREEN}OK{RESET} ingest_token minted "
        f"(hash prefix {token_hash[:8]}...)",
    )
    return plaintext


async def call_resolve_gate_rpc(
    request_id: uuid.UUID, decision: str
) -> dict[str, Any]:
    """Call resolve_gate RPC as superuser (admin path)."""
    token_hmac = hashlib.sha256(
        f"livetest-{request_id}".encode("utf-8")
    ).hexdigest()
    import json as _json

    conn = await _connect_pg()
    try:
        result = await conn.fetchval(
            "SELECT public.resolve_gate($1, $2, $3, $4, $5, $6, $7)",
            SEED_TENANT_ID,
            request_id,
            decision,
            SEED_USER_ID,
            token_hmac,
            None,
            "platform",
        )
        # asyncpg may return jsonb as a string or a dict depending on
        # codec setup — normalize both.
        if isinstance(result, str):
            return _json.loads(result)
        if isinstance(result, dict):
            return result
        return {"raw": str(result)}
    finally:
        await conn.close()


async def fetch_decision_row(request_id: uuid.UUID) -> dict[str, Any] | None:
    """SELECT the gate_decisions row for a given request_id."""
    conn = await _connect_pg()
    try:
        row = await conn.fetchrow(
            "SELECT request_id, decision, decided_by, by_source, decided_at "
            "FROM public.gate_decisions WHERE request_id=$1 LIMIT 1",
            request_id,
        )
        return dict(row) if row else None
    finally:
        await conn.close()


async def probe_bridge_resolution(
    request_id: uuid.UUID, bearer: str
) -> dict[str, Any]:
    """GET /api/v1/bridge/gates/:id/resolution to observe platform state."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            f"{PLATFORM_BASE_URL}/api/v1/bridge/gates/{request_id}/resolution",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text[:200]}
        return {"status": resp.status_code, "body": body}


# --- Flows ---------------------------------------------------------------
async def flow_happy_path(bearer: str) -> None:
    """Flow 1: gate created, admin decides via RPC, wait_for returns granted."""
    _log("flow-1", "happy path: admin decides, SDK observes granted")

    from codeatelier_governance import GovernanceSDK

    sdk = GovernanceSDK(
        api_key="livetest-no-db",  # dummy; in-memory stores everywhere
        audit_secret=secrets.token_bytes(32),
        platform_bridge_enabled=True,
        platform_ingest_url=f"{PLATFORM_BASE_URL}/api/v1/ingest/events",
        platform_ingest_token=bearer,
        warn_on_no_wrappers=False,
    )
    async with sdk:
        req = await sdk.gates.request(
            "refund.issue", "agent-livetest", payload={"amount": 4800}
        )
        _log("flow-1", f"created gate {req.request_id}")

        # Give forward a chance to land.
        await asyncio.sleep(1.0)

        probe = await probe_bridge_resolution(req.request_id, bearer=bearer)
        _log("flow-1", f"bridge GET -> HTTP {probe['status']}: {probe['body']}")
        assert probe["status"] == 200, f"bridge GET unexpected: {probe}"
        assert probe["body"]["status"] in ("pending", "expired"), probe

        # Simulate admin click via resolve_gate RPC.
        _log("flow-1", "calling resolve_gate RPC as admin...")
        result = await call_resolve_gate_rpc(req.request_id, decision="granted")
        _log("flow-1", f"RPC result: {result}")
        assert result.get("status") == "ok", result

        # wait_for should now observe the platform decision.
        granted = await sdk.gates.wait_for(req.request_id, timeout=5.0)
        _log("flow-1", f"wait_for returned granted={granted} (expected True)")
        assert granted is True

        # Audit chain: find approval.granted with by=platform.
        from codeatelier_governance.audit.store import InMemoryAuditStore

        store = sdk.audit._store  # type: ignore[attr-defined]
        assert isinstance(store, InMemoryAuditStore)
        events = list(store._events.values())  # type: ignore[attr-defined]
        platform_granted = [
            e
            for e in events
            if e.kind == "approval.granted"
            and e.metadata.get("by") == "platform"
        ]
        _log("flow-1", f"audit rows with by=platform: {len(platform_granted)}")
        assert platform_granted, (
            f"no by=platform audit row; kinds={[e.kind for e in events]}"
        )
    _log("flow-1", f"{GREEN}PASS{RESET}")


async def flow_local_wins(bearer: str) -> None:
    """Flow 2: gate forwarded, grant locally, reverse-sync fires."""
    _log("flow-2", "local wins: grant before admin, reverse-sync hits platform")

    from codeatelier_governance import GovernanceSDK

    sdk = GovernanceSDK(
        api_key="livetest-no-db",  # dummy; in-memory stores everywhere
        audit_secret=secrets.token_bytes(32),
        platform_bridge_enabled=True,
        platform_ingest_url=f"{PLATFORM_BASE_URL}/api/v1/ingest/events",
        platform_ingest_token=bearer,
        warn_on_no_wrappers=False,
    )
    async with sdk:
        req = await sdk.gates.request(
            "delete.user", "agent-livetest", payload={"user_id": "u1"}
        )
        _log("flow-2", f"created gate {req.request_id}")

        # Wait for forward to ack so reverse-sync can see the gate.
        pc = sdk._platform_client
        assert pc is not None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if pc.stats()["gate_requests_sent"] >= 1:
                break
            await asyncio.sleep(0.1)
        _log(
            "flow-2",
            f"forward ack'd: sent={pc.stats()['gate_requests_sent']}"
            f" dropped={pc.stats()['gate_requests_dropped']}",
        )

        # Grant locally.
        await sdk.gates.grant(req.token)
        _log("flow-2", "granted locally; expect reverse-sync POST next")

        # wait_for immediately resolves from local.
        granted = await sdk.gates.wait_for(req.request_id, timeout=2.0)
        assert granted is True

        # Give reverse-sync time to land.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if pc.stats()["local_resolutions_sent"] >= 1:
                break
            await asyncio.sleep(0.2)
        stats = pc.stats()
        _log(
            "flow-2",
            f"stats: local_resolutions_sent={stats['local_resolutions_sent']}"
            f" local_resolutions_dropped={stats['local_resolutions_dropped']}",
        )
        assert stats["local_resolutions_sent"] >= 1, (
            f"reverse-sync did not fire: {stats}"
        )

        # Verify the platform's gate_decisions row has by_source='sdk_local'.
        await asyncio.sleep(0.5)
        row = await fetch_decision_row(req.request_id)
        _log("flow-2", f"gate_decisions row: {row}")
        assert row is not None, "no gate_decisions row after reverse-sync"
        assert row["by_source"] == "sdk_local", row
        assert row["decided_by"] is None, row
    _log("flow-2", f"{GREEN}PASS{RESET}")


async def flow_platform_down(bearer: str) -> None:
    """Flow 3: platform unreachable -> local grant still works; polls fail."""
    _log("flow-3", "platform-down path: local-only resolution, polls count")

    from codeatelier_governance import GovernanceSDK

    # Deliberately unreachable port — simulates platform outage without
    # killing the dev server (so the suite stays reproducible).
    unreachable = "http://127.0.0.1:59999"
    sdk = GovernanceSDK(
        api_key="livetest-no-db",  # dummy; in-memory stores everywhere
        audit_secret=secrets.token_bytes(32),
        platform_bridge_enabled=True,
        platform_ingest_url=f"{unreachable}/api/v1/ingest/events",
        platform_ingest_token=bearer,
        warn_on_no_wrappers=False,
    )
    async with sdk:
        req = await sdk.gates.request(
            "patient.archive", "agent-livetest", payload={}
        )
        _log("flow-3", f"created gate {req.request_id}")

        await sdk.gates.grant(req.token)
        granted = await sdk.gates.wait_for(req.request_id, timeout=2.0)
        assert granted is True
        _log("flow-3", "local grant resolved wait_for without platform")

        pc = sdk._platform_client
        assert pc is not None
        # Trigger an explicit failed poll to bump the counter.
        try:
            await pc.poll_gate_resolution(uuid.uuid4())
        except Exception as exc:
            _log("flow-3", f"poll raised as expected: {type(exc).__name__}")

        stats = pc.stats()
        _log(
            "flow-3",
            f"stats: gate_polls_failed={stats['gate_polls_failed']}"
            f" gate_requests_dropped={stats['gate_requests_dropped']}",
        )
        assert stats["gate_polls_failed"] >= 1, stats
    _log("flow-3", f"{GREEN}PASS{RESET}")


# --- Main ----------------------------------------------------------------
async def main() -> int:
    print(f"\n{CYAN}=== live_test_platform_bridge ==={RESET}")
    preflight()
    await preflight_db()
    await ensure_service_role_grants()
    await ensure_auth_user()
    bearer = await seed_ingest_token()

    for label, flow in (
        ("flow-1", flow_happy_path),
        ("flow-2", flow_local_wins),
        ("flow-3", flow_platform_down),
    ):
        try:
            await flow(bearer)
        except AssertionError as exc:
            _log(label, f"{RED}FAIL{RESET} {exc}", color=RED)
            return 1
        except Exception as exc:
            _log(
                label,
                f"{RED}ERROR{RESET} {type(exc).__name__}: {exc}",
                color=RED,
            )
            return 1

    print(f"\n  {GREEN}ALL FLOWS PASSED{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
