"""Seed script for Video 1 — Article 12 walkthrough.

Populates a Postgres database with the camera-ready demo state the
UI/UX team speced for the 90-second Article 12 walkthrough:

* 4 agents in /agents (invoice-approver, refund-bot, contract-drafter
  [halted], research-agent [degraded])
* ~245 Ed25519-signed + HMAC-chained audit events
* 1 mid-history chain-key rotation marker with dual MACs
* 1 pending HITL approval in /approvals
* verify_chain returns ok on the full history
* Total events >200 so the compliance pill shows a meaningful count

Usage::

    python scripts/promo/video1_article12/seed.py \\
        --database-url postgresql://governance:governance@localhost:5435/gov_video1_seed \\
        --audit-secret "$(python -c 'import secrets; print(secrets.token_hex(32))')"

The script dogfoods the SDK surface wherever possible: every audit event
flows through ``sdk.audit.log(AuditEvent(...))`` so the HMAC chain and
Ed25519 signature are correct. Raw SQL is used only for (a) truncating
tables before re-seeding (append-only triggers block DELETE) and (b)
writing the halt-switch metadata marker which has no public SDK method.

This is a PROMO seed, not production code. It tolerates laxer lint than
the src/ tree (module-level wall-clock monkeypatches, synthetic fixtures,
etc.) — the CLAUDE.md invariants about prod code DO NOT bind here.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4


# --- stdlib-only helpers we need before importing the SDK --------------------


def _normalize_async_url(url: str) -> str:
    """Convert postgresql:// to postgresql+asyncpg:// for SQLAlchemy async."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


# ----------------------------------------------------------------------------
# Demo spec constants. Every number here is load-bearing for the UI/UX video.
# ----------------------------------------------------------------------------

INVOICE_APPROVER = "invoice-approver"
REFUND_BOT = "refund-bot"
CONTRACT_DRAFTER = "contract-drafter"
RESEARCH_AGENT = "research-agent"

# Per-agent event counts. Total = 245 (120 + 60 + 40 + 25). Refer to the
# Demo spec in the task brief. `contract-drafter` has 40 pre-halt events
# PLUS one `agent.halted` marker → 41 rows.
EVENT_COUNTS: dict[str, int] = {
    INVOICE_APPROVER: 120,
    REFUND_BOT: 60,
    CONTRACT_DRAFTER: 40,
    RESEARCH_AGENT: 25,
}

# Time window the demo script simulates. All events stamp within this
# window via the monkeypatched clock below.
NOW = datetime.now(timezone.utc)
ORIGIN = NOW - timedelta(days=30)

# Halt marker timestamp for contract-drafter. Must predate NOW by ~2h so
# the UI renders "halted 2 hours ago".
HALT_AT = NOW - timedelta(hours=2)


# ----------------------------------------------------------------------------
# Clock control. The SDK's audit.log path uses ``datetime.now(timezone.utc)``
# from ``codeatelier_governance.audit.module`` — we monkeypatch that single
# module-level reference so our seeded events land at fake historical
# timestamps without UPDATE-ing rows (the append-only trigger would block).
#
# We do NOT patch ``datetime.datetime.now`` globally because SQLAlchemy +
# presence heartbeat rely on the real clock for their own timestamps.
# ----------------------------------------------------------------------------


class _FakeClock:
    """Returns the staged ``current`` datetime from ``datetime.now(tz)``."""

    def __init__(self) -> None:
        self.current: datetime = NOW

    def __call__(self, *_args: Any, **_kwargs: Any) -> datetime:
        return self.current


def _install_fake_clock() -> _FakeClock:
    """Replace ``audit.module.datetime`` with a shim that returns fake nows.

    Returns the clock so callers can advance it between ``sdk.audit.log``
    calls. Reversible via ``_restore_real_clock`` below.
    """
    import codeatelier_governance.audit.module as audit_module

    real_datetime = audit_module.datetime
    clock = _FakeClock()

    class _ShimDatetime:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            if tz is None:
                return clock.current.replace(tzinfo=None)
            return clock.current.astimezone(tz)

        # Preserve every other attribute (timedelta users, fromtimestamp, …)
        def __getattr__(self, name: str) -> Any:
            return getattr(real_datetime, name)

    # The audit module imports ``datetime`` as a *class*, not a module, so
    # we only need to swap that single reference.
    audit_module.datetime = _ShimDatetime()  # type: ignore[assignment]
    audit_module._SEED_REAL_DATETIME = real_datetime  # type: ignore[attr-defined]
    return clock


def _restore_real_clock() -> None:
    import codeatelier_governance.audit.module as audit_module

    real_dt = getattr(audit_module, "_SEED_REAL_DATETIME", None)
    if real_dt is not None:
        audit_module.datetime = real_dt


# ----------------------------------------------------------------------------
# Event scripts. Each function returns the ``kind`` + ``metadata`` pair for
# an event at sequence ``i`` in the agent's timeline. Synthetic but
# plausible — no real LLM calls, no real PII.
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class EventScript:
    agent_id: str
    tools: list[str]
    model: str
    kinds: list[str]


SCRIPTS: dict[str, EventScript] = {
    INVOICE_APPROVER: EventScript(
        agent_id=INVOICE_APPROVER,
        tools=["read_invoice", "send_email", "create_draft"],
        model="gpt-4o-mini",
        kinds=["agent.start", "tool.call", "tool.result", "llm.call", "llm.result", "agent.end"],
    ),
    REFUND_BOT: EventScript(
        agent_id=REFUND_BOT,
        tools=["lookup_txn", "refund_order"],
        model="gpt-4o-mini",
        kinds=["agent.start", "tool.call", "tool.result", "llm.call", "llm.result", "agent.end"],
    ),
    CONTRACT_DRAFTER: EventScript(
        agent_id=CONTRACT_DRAFTER,
        tools=["read_contract", "draft_section"],
        model="gpt-4o-mini",
        kinds=["agent.start", "tool.call", "tool.result", "llm.call", "llm.result", "agent.end"],
    ),
    RESEARCH_AGENT: EventScript(
        agent_id=RESEARCH_AGENT,
        tools=["search_docs"],
        model="gpt-4o-mini",
        kinds=["agent.start", "tool.call", "tool.result", "llm.call", "llm.result", "agent.end"],
    ),
}


# ----------------------------------------------------------------------------
# Phase 0: migrate schema.
# ----------------------------------------------------------------------------


async def phase_migrate(database_url: str) -> None:
    print(f"[phase 0] migrate schema against {database_url}")
    # Dogfood the SDK's own migrator. It applies DDL + alembic head.
    from codeatelier_governance.cli.commands import _run_migrate

    await _run_migrate(database_url)


# ----------------------------------------------------------------------------
# Phase 1: wipe old seed data (append-only triggers block DELETE, so we
# TRUNCATE — which is NOT trigger-controlled — and drop the chain-keys /
# agent-keys / revocations rows too).
# ----------------------------------------------------------------------------


_TABLES_TO_WIPE = (
    "governance_audit_events",
    "governance_agent_presence",
    "governance_policies",
    "governance_gates_pending",
    "governance_agent_keys",
    "governance_agent_key_revocations",
    "governance_audit_chain_keys",
    "governance_cost_session_usage",
    "governance_cost_agent_daily",
    "governance_cost_model_daily",
)


async def phase_wipe(database_url: str) -> None:
    print("[phase 1] wipe previous seed data via TRUNCATE CASCADE")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_normalize_async_url(database_url))
    try:
        # Probe which tables exist — the SDK's migrate currently doesn't
        # ship DDL for ``governance_policies`` (that table is created on
        # first policy upsert via ON CONFLICT). We only TRUNCATE tables
        # that exist, CREATE policies on demand below.
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename = ANY(:names)"
                ),
                {"names": list(_TABLES_TO_WIPE)},
            )
            existing = [row[0] for row in res]
        async with engine.begin() as conn:
            # Create governance_policies if migrate didn't. The scope/cost
            # modules both upsert into this table; without it the
            # register() calls log a warning and silently succeed (Pydantic
            # model stays in-memory) so live-test still works, BUT the
            # console wouldn't show any policy rows on /agents.
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS governance_policies ("
                "  agent_id VARCHAR(256) NOT NULL,"
                "  policy_type VARCHAR(32) NOT NULL,"
                "  policy_json JSONB NOT NULL,"
                "  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
                "  PRIMARY KEY (agent_id, policy_type)"
                ")"
            ))
            if "governance_policies" not in existing:
                existing.append("governance_policies")
            if not existing:
                return
            # TRUNCATE bypasses the row-level append-only triggers (which
            # only fire on UPDATE / DELETE). RESTART IDENTITY resets the
            # BIGSERIAL chain_seq so the first re-seeded event lands at 1.
            tables = ", ".join(existing)
            await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


# ----------------------------------------------------------------------------
# Phase 2: bootstrap SDK with Ed25519 signing enabled + chain-key registry.
# ----------------------------------------------------------------------------


async def phase_bootstrap_keys(
    database_url: str,
    audit_secret: bytes,
) -> None:
    """Register the INITIAL chain key in ``governance_audit_chain_keys``.

    Without this row, ``rotate_chain_key`` in phase 5 raises
    ``'outgoing fingerprint is not registered'`` because the rotation path
    looks the outgoing version up by fingerprint. The SDK's normal audit-
    log path does NOT touch the chain-keys registry, so we have to insert
    the bootstrap row ourselves.
    """
    print("[phase 2] bootstrap chain-key registry with initial v1 fingerprint")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from codeatelier_governance.audit.keys import fingerprint_key

    engine = create_async_engine(_normalize_async_url(database_url))
    try:
        fp = fingerprint_key(audit_secret)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO governance_audit_chain_keys "
                    "(fingerprint, activated_at_chain_seq) "
                    "VALUES (:fp, 0) "
                    "ON CONFLICT (fingerprint) DO NOTHING"
                ),
                {"fp": fp},
            )
    finally:
        await engine.dispose()


# ----------------------------------------------------------------------------
# Phase 3: construct SDK with Ed25519 enabled + register policies.
# ----------------------------------------------------------------------------


def _build_sdk(database_url: str, audit_secret: bytes) -> Any:
    """Construct the SDK with Ed25519 agent identity enabled (ephemeral key).

    ``enabled=True`` + ``key_source='ephemeral'`` makes the SDK generate a
    throwaway Ed25519 keypair in-process and sign every audit row. Every
    seeded row lands with ``signature_status='signed'``.
    """
    from codeatelier_governance import GovernanceSDK
    from codeatelier_governance.identity.config import AgentIdentityConfig

    return GovernanceSDK(
        database_url=database_url,
        audit_secret=audit_secret,
        warn_on_no_wrappers=False,
        agent_identity_config=AgentIdentityConfig(
            enabled=True, key_source="ephemeral",
        ),
    )


def _register_policies(sdk: Any) -> None:
    print("[phase 3] register scope + budget policies for all 4 agents")
    from codeatelier_governance.cost.models import BudgetPolicy
    from codeatelier_governance.scope.models import ScopePolicy

    sdk.scope.register(ScopePolicy(
        agent_id=INVOICE_APPROVER,
        allowed_tools=frozenset({"read_invoice", "send_email", "create_draft"}),
    ))
    sdk.scope.register(ScopePolicy(
        agent_id=REFUND_BOT,
        allowed_tools=frozenset({"lookup_txn", "refund_order"}),
    ))
    sdk.scope.register(ScopePolicy(
        agent_id=CONTRACT_DRAFTER,
        allowed_tools=frozenset({"read_contract", "draft_section"}),
    ))
    sdk.scope.register(ScopePolicy(
        agent_id=RESEARCH_AGENT,
        allowed_tools=frozenset({"search_docs"}),
    ))

    sdk.cost.register(BudgetPolicy(
        agent_id=INVOICE_APPROVER, per_session_usd=5.0, per_agent_usd_daily=25.0,
    ))
    sdk.cost.register(BudgetPolicy(
        agent_id=REFUND_BOT, per_session_usd=5.0, per_agent_usd_daily=25.0,
    ))
    sdk.cost.register(BudgetPolicy(
        agent_id=CONTRACT_DRAFTER, per_session_usd=5.0, per_agent_usd_daily=25.0,
    ))
    sdk.cost.register(BudgetPolicy(
        agent_id=RESEARCH_AGENT, per_session_usd=5.0, per_agent_usd_daily=25.0,
    ))


# ----------------------------------------------------------------------------
# Phase 4: seed audit events with monkeypatched clock.
# ----------------------------------------------------------------------------


def _fake_hash(label: str, i: int) -> str:
    return hashlib.sha256(f"{label}:{i}".encode("utf-8")).hexdigest()


async def _seed_agent_events(
    sdk: Any,
    clock: _FakeClock,
    script: EventScript,
    total: int,
    t0: datetime,
    t1: datetime,
) -> int:
    """Seed ``total`` events for one agent, spread evenly over ``[t0, t1]``.

    Events are emitted in groups of ``len(script.kinds)`` sharing a
    session_id so the UI can render session-scoped traces. Every event
    flows through ``sdk.audit.log`` — HMAC chain + Ed25519 signature are
    therefore correct end-to-end.

    Returns the number of events actually logged (should equal ``total``).
    """
    from codeatelier_governance.audit.models import AuditEvent

    rng = random.Random(hash(script.agent_id) & 0xFFFFFFFF)
    window = (t1 - t0).total_seconds()
    kinds = script.kinds
    # Build a flat list of event specs so timestamps distribute cleanly.
    specs: list[tuple[str, dict[str, Any]]] = []
    sessions_needed = (total + len(kinds) - 1) // len(kinds)
    for s in range(sessions_needed):
        sid = uuid4()
        for j, k in enumerate(kinds):
            if len(specs) >= total:
                break
            tool = rng.choice(script.tools)
            meta: dict[str, Any] = {
                "session_seq": s,
                "step": j,
            }
            if k == "tool.call":
                meta["tool"] = tool
            if k == "tool.result":
                meta["tool"] = tool
                meta["ok"] = True
            if k == "llm.call":
                meta["tokens_estimated"] = 200 + rng.randint(0, 800)
            if k == "llm.result":
                meta["tokens_in"] = 180 + rng.randint(0, 400)
                meta["tokens_out"] = 60 + rng.randint(0, 200)
            specs.append((k, meta | {"_sid": sid}))

    # Drop any overshoot.
    specs = specs[:total]
    logged = 0
    for idx, (kind, meta) in enumerate(specs):
        # Distribute timestamps linearly so the UI's last_active timeline
        # looks continuous over the 30-day window.
        frac = idx / max(len(specs) - 1, 1)
        clock.current = t0 + timedelta(seconds=window * frac)
        sid = meta.pop("_sid")
        event = AuditEvent(
            session_id=sid,
            agent_id=script.agent_id,
            kind=kind,
            model=script.model if kind.startswith("llm.") else None,
            input_hash=_fake_hash(f"in:{script.agent_id}", idx) if kind.endswith("call") else None,
            output_hash=_fake_hash(f"out:{script.agent_id}", idx) if kind.endswith("result") else None,
            metadata=meta,
        )
        await sdk.audit.log(event)
        logged += 1
    return logged


async def _seed_research_agent_violation(
    sdk: Any, clock: _FakeClock, at: datetime,
) -> None:
    """Emit the ``scope.violation`` event for research-agent (degraded pill).

    The UI spec calls for one session ending with ``tool=execute_sql`` not
    in scope. We dogfood the SDK: ``sdk.scope.check`` auto-logs the
    violation event (the SCOPE_VIOLATION kind) on failure.
    """
    from codeatelier_governance.scope.errors import ScopeViolation

    clock.current = at
    try:
        await sdk.scope.check(RESEARCH_AGENT, tool="execute_sql")
    except ScopeViolation:
        # Expected — the violation event was logged as a side effect of the
        # check() failing. This is the documented dogfood path.
        pass


# ----------------------------------------------------------------------------
# Phase 5: mid-history chain-key rotation.
# ----------------------------------------------------------------------------


async def phase_rotate(
    database_url: str, outgoing_secret: bytes, incoming_secret: bytes,
) -> tuple[int, str]:
    """Rotate the HMAC chain key and return (marker_chain_seq, incoming_fp)."""
    print("[phase 5] rotate HMAC chain key (dual-signed marker row)")
    from sqlalchemy.ext.asyncio import create_async_engine

    from codeatelier_governance.audit.rotation import rotate_chain_key

    engine = create_async_engine(_normalize_async_url(database_url))
    try:
        result = await rotate_chain_key(
            engine,
            outgoing_secret=outgoing_secret,
            incoming_secret=incoming_secret,
            operator_id="compliance-officer@demo",
            rotation_reason="scheduled",
        )
    finally:
        await engine.dispose()
    return result.marker_chain_seq, result.incoming_fingerprint


# ----------------------------------------------------------------------------
# Phase 6: halt contract-drafter (writes the v0.6 halt marker into
# governance_agent_presence.metadata_json). No public SDK surface — the
# console's /halt endpoint is the canonical path, but for a headless seed
# we bypass it and write the marker via the same SQL the endpoint runs.
# Documented with a comment pointing at the endpoint source.
# ----------------------------------------------------------------------------


async def phase_halt_contract_drafter(database_url: str) -> None:
    print(f"[phase 6] halt {CONTRACT_DRAFTER} (marker + agent.halted audit row)")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_normalize_async_url(database_url))
    try:
        halt_meta = json.dumps({
            "_halted_by": "compliance-officer@demo",
            "_halted_at": HALT_AT.isoformat(),
            "_halt_reason": "anomaly detected",
        })
        async with engine.begin() as conn:
            # Upsert presence row with halt marker (mirrors the console's
            # halt_agent handler in src/codeatelier_governance/console/app.py:2061).
            await conn.execute(
                text(
                    "INSERT INTO governance_agent_presence "
                    "(agent_id, status, last_heartbeat, started_at, metadata_json) "
                    "VALUES (:aid, 'unresponsive', NOW(), NOW(), CAST(:meta AS jsonb)) "
                    "ON CONFLICT (agent_id) DO UPDATE SET "
                    "status = 'unresponsive', metadata_json = CAST(:meta AS jsonb)"
                ),
                {"aid": CONTRACT_DRAFTER, "meta": halt_meta},
            )
    finally:
        await engine.dispose()


async def _emit_halt_audit_event(sdk: Any, clock: _FakeClock) -> None:
    """Emit the ``agent.halted`` audit row tied to contract-drafter."""
    from codeatelier_governance.audit.models import AuditEvent

    clock.current = HALT_AT
    sid = UUID(
        hashlib.md5(
            ("halt:" + CONTRACT_DRAFTER + ":" + HALT_AT.isoformat()).encode(),
        ).hexdigest(),
    )
    await sdk.audit.log(AuditEvent(
        session_id=sid,
        agent_id=CONTRACT_DRAFTER,
        kind="agent.halted",
        metadata={
            "halted_by": "compliance-officer@demo",
            "reason": "anomaly detected",
            "halted_at": HALT_AT.isoformat(),
        },
    ))


# ----------------------------------------------------------------------------
# Phase 7: pending HITL approval.
# ----------------------------------------------------------------------------


async def phase_pending_approval(sdk: Any) -> UUID:
    print("[phase 7] create 1 pending HITL approval for invoice-approver")
    req = await sdk.gates.request(
        kind="send_email",
        agent_id=INVOICE_APPROVER,
        payload={
            "rationale": "Auto-approve threshold exceeded for invoice total > $10k",
            "tool": "send_email",
            "invoice_total_usd": 12_400.00,
        },
    )
    return req.request_id


# ----------------------------------------------------------------------------
# Phase 8: presence heartbeats for the three live-ish agents.
# ----------------------------------------------------------------------------


async def phase_presence(sdk: Any) -> None:
    print("[phase 8] heartbeat live agents (invoice-approver, refund-bot, research-agent)")
    # research-agent is "degraded" — we still heartbeat but status stays
    # 'live' at the row level; the UI derives the degraded pill from the
    # presence of scope.violation events, not from presence.status.
    await sdk.presence.heartbeat(INVOICE_APPROVER, operator_id="demo-operator@demo")
    await sdk.presence.heartbeat(REFUND_BOT, operator_id="demo-operator@demo")
    await sdk.presence.heartbeat(RESEARCH_AGENT, operator_id="demo-operator@demo")
    # contract-drafter row was written by phase_halt above; leave alone.


# ----------------------------------------------------------------------------
# Phase 9: cost-track some usage so the budget pill reads 62% for
# invoice-approver. 62% of $5/session = $3.10 across 2 sessions today.
# ----------------------------------------------------------------------------


async def phase_seed_cost(sdk: Any) -> None:
    print("[phase 9] cost-track recent usage for budget pill (62% on invoice-approver)")
    # Two sessions today for invoice-approver, each ~$1.55 so session
    # usage hits ~$3.10 → 62% of $5 cap, matching the UI spec.
    s1, s2 = uuid4(), uuid4()
    await sdk.cost.track(INVOICE_APPROVER, s1, usd=1.55, tokens=18_000, model="gpt-4o-mini")
    await sdk.cost.track(INVOICE_APPROVER, s2, usd=1.55, tokens=18_000, model="gpt-4o-mini")
    # refund-bot: 15% used → ~$0.75 one session.
    s3 = uuid4()
    await sdk.cost.track(REFUND_BOT, s3, usd=0.75, tokens=8_000, model="gpt-4o-mini")


# ----------------------------------------------------------------------------
# Phase 10: verify chain + final summary.
# ----------------------------------------------------------------------------


async def phase_verify_and_report(
    sdk: Any,
    database_url: str,
    marker_chain_seq: int,
    pending_request_id: UUID,
) -> None:
    print("[phase 10] verify chain end-to-end and report")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    # Count rows per agent + signature status breakdown.
    engine = create_async_engine(_normalize_async_url(database_url))
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT agent_id, COUNT(*) FROM governance_audit_events "
                    "GROUP BY agent_id ORDER BY agent_id"
                ),
            )
            per_agent = list(res)

            res = await conn.execute(
                text(
                    "SELECT signature_status, COUNT(*) "
                    "FROM governance_audit_events GROUP BY signature_status"
                ),
            )
            by_status = list(res)

            res = await conn.execute(
                text("SELECT COUNT(*) FROM governance_audit_events"),
            )
            total = int(res.scalar_one())

            res = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM governance_audit_events "
                    "WHERE kind = 'audit.chain_key_rotation'"
                ),
            )
            rotation_rows = int(res.scalar_one())

            res = await conn.execute(
                text(
                    "SELECT request_id FROM governance_gates_pending "
                    "WHERE resolved_at IS NULL"
                ),
            )
            pending = list(res)
    finally:
        await engine.dispose()

    # Dogfood: use the SDK's compliance report generator — the same code
    # path the console's compliance pill hits — to run the rotation-aware
    # verifier against the full history. If it doesn't return 'verified',
    # FAIL LOUDLY.
    from codeatelier_governance.audit.keys import clear_key_cache
    from codeatelier_governance.compliance.report import ReportGenerator

    # We advanced clock during seeding but the verify path computes HMACs
    # from stored created_at values, so it's fine to leave the shim in
    # place. Still, restore the real clock before the verify pass so the
    # ``ok|ok|ok`` audit events logged by the compliance path (if any)
    # land at true NOW.
    _restore_real_clock()
    clear_key_cache()

    generator = ReportGenerator(
        database_url=_normalize_async_url(database_url),
        audit_module=sdk.audit,
    )
    report = await generator.generate_article12(verify_chain=True)
    chain_status = report.chain_integrity_status

    # -----------------------------------------------------------------
    # Final summary
    # -----------------------------------------------------------------
    print()
    print("=" * 72)
    print("Video 1 seed complete")
    print("=" * 72)
    print(f"  total audit events      : {total}")
    for agent_id, count in per_agent:
        print(f"    {agent_id:22s}: {count}")
    print("  signature_status rows   :")
    for status, count in by_status:
        print(f"    {status:22s}: {count}")
    print(f"  chain-key rotation rows : {rotation_rows} (marker at chain_seq={marker_chain_seq})")
    print(f"  pending HITL requests   : {len(pending)}  (req_id={pending_request_id})")
    print(f"  verify_chain (rotation) : {chain_status}")
    print(f"  rotation_aware flag     : {report.rotation_aware}")
    print()
    print("  Expected /agents pills  :")
    print(f"    {INVOICE_APPROVER:22s}: verified, live, budget 62% used, 2 sessions/24h")
    print(f"    {REFUND_BOT:22s}: verified, live, budget 15% used")
    print(f"    {CONTRACT_DRAFTER:22s}: halted (red), reason 'anomaly detected', by compliance-officer@demo")
    print(f"    {RESEARCH_AGENT:22s}: degraded (scope.violation on execute_sql)")
    print()
    print("  Pill state source code references:")
    print("    console.app:list_agents    → src/codeatelier_governance/console/app.py:1097")
    print("    console.app:halt_agent     → src/codeatelier_governance/console/app.py:2061")
    print("    console.app:_map_chain_status → src/codeatelier_governance/console/app.py:3328")
    print("    compliance.report.ReportGenerator → src/codeatelier_governance/compliance/report.py")
    print("    audit.module.AuditModule.log → src/codeatelier_governance/audit/module.py:247")
    print()

    # Fail LOUD on broken chain. The whole point of the script is to leave
    # the console camera-ready; a chain that verifies anything other than
    # 'verified' means we shipped a broken demo.
    if chain_status != "verified":
        print(
            f"ERROR: chain verification returned status={chain_status!r}, "
            f"expected 'verified'. Seed FAILED.",
            file=sys.stderr,
        )
        sys.exit(1)
    print("OK. Chain verified. Console is camera-ready.")


# ----------------------------------------------------------------------------
# Top-level orchestration.
# ----------------------------------------------------------------------------


async def _run(database_url: str, audit_secret_hex: str) -> None:
    if len(audit_secret_hex) < 64:
        raise ValueError(
            "audit-secret must be at least 32 bytes (64 hex chars). "
            "Generate with: python -c 'import secrets; print(secrets.token_hex(32))'"
        )
    outgoing_secret = audit_secret_hex.encode("utf-8")
    # The SDK requires the HMAC secret via env OR constructor kwarg. We
    # pass via constructor to avoid leaking into the shell env.

    await phase_migrate(database_url)
    await phase_wipe(database_url)
    await phase_bootstrap_keys(database_url, outgoing_secret)

    # Install the fake clock NOW so every audit.log call lands at a staged
    # historical time. The clock starts at ORIGIN (30 days ago) and moves
    # forward as we seed.
    clock = _install_fake_clock()

    sdk = _build_sdk(database_url, outgoing_secret)
    async with sdk:
        _register_policies(sdk)

        # Seed pre-rotation events: a chunk of invoice-approver + a chunk
        # of refund-bot over the first ~15 days so the rotation marker
        # lands cleanly in the middle of the history. After rotation we
        # seed the rest.
        print("[phase 4a] seed pre-rotation audit events")
        pre_count_invoice = EVENT_COUNTS[INVOICE_APPROVER] // 2
        pre_count_refund = EVENT_COUNTS[REFUND_BOT] // 2
        mid = NOW - timedelta(days=15)
        await _seed_agent_events(
            sdk, clock, SCRIPTS[INVOICE_APPROVER], pre_count_invoice,
            t0=ORIGIN, t1=mid,
        )
        await _seed_agent_events(
            sdk, clock, SCRIPTS[REFUND_BOT], pre_count_refund,
            t0=ORIGIN, t1=mid,
        )
        # Seed contract-drafter's 40 pre-halt events before the halt
        # moment (HALT_AT = NOW - 2h) so they fit in the window and the
        # agent.halted row lands LAST for this agent.
        await _seed_agent_events(
            sdk, clock, SCRIPTS[CONTRACT_DRAFTER],
            EVENT_COUNTS[CONTRACT_DRAFTER],
            t0=ORIGIN + timedelta(days=1), t1=HALT_AT - timedelta(hours=1),
        )

        # The rotation marker is a DIRECT INSERT into governance_audit_events
        # (the dogfood path). We close the SDK first so no other writes
        # race the rotation transaction.
    # end `async with sdk` — flushes writer.

    # Phase 5: rotate mid-history.
    incoming_secret = secrets.token_hex(32).encode("utf-8")
    marker_seq, incoming_fp = await phase_rotate(
        database_url, outgoing_secret, incoming_secret,
    )
    print(f"  → marker at chain_seq={marker_seq}, incoming_fp={incoming_fp[:16]}...")

    # Phase 4b: seed post-rotation events with the NEW HMAC secret. The
    # SDK needs a fresh instance keyed under the incoming secret for these
    # rows to HMAC correctly against the post-rotation key.
    print("[phase 4b] seed post-rotation audit events under incoming key")
    clock = _install_fake_clock()
    sdk2 = _build_sdk(database_url, incoming_secret)
    async with sdk2:
        _register_policies(sdk2)
        post_count_invoice = EVENT_COUNTS[INVOICE_APPROVER] - pre_count_invoice
        post_count_refund = EVENT_COUNTS[REFUND_BOT] - pre_count_refund
        await _seed_agent_events(
            sdk2, clock, SCRIPTS[INVOICE_APPROVER], post_count_invoice,
            t0=mid, t1=NOW - timedelta(hours=1),
        )
        await _seed_agent_events(
            sdk2, clock, SCRIPTS[REFUND_BOT], post_count_refund,
            t0=mid, t1=NOW - timedelta(hours=1),
        )
        # research-agent events (25): all post-rotation so the violation
        # event sits on the latest key.
        await _seed_agent_events(
            sdk2, clock, SCRIPTS[RESEARCH_AGENT], EVENT_COUNTS[RESEARCH_AGENT] - 1,
            t0=mid, t1=NOW - timedelta(hours=2),
        )
        # Research-agent: one scope.violation via the dogfood scope.check
        # path. This logs the 25th event.
        await _seed_research_agent_violation(
            sdk2, clock, at=NOW - timedelta(hours=1, minutes=45),
        )

        # Phase 6 audit event: emit agent.halted row (via SDK) before
        # writing the presence marker. The presence marker is a bare SQL
        # insert that lives outside the chain.
        await _emit_halt_audit_event(sdk2, clock)
        await phase_presence(sdk2)
        await phase_seed_cost(sdk2)
        pending_request_id = await phase_pending_approval(sdk2)

        # The chain-key resolution cache survives across SDK instances in
        # this process. Clear it so the verify pass picks up the NEW key
        # without restarting.
        from codeatelier_governance.audit.keys import clear_key_cache
        clear_key_cache()

        # Point the verifier at both secrets so it can verify both
        # pre- AND post-rotation rows. The ReportGenerator's _build_uri_map
        # scans env vars named GOVERNANCE_CHAIN_KEY_<fingerprint_first16>
        # and builds its map from them (see
        # src/codeatelier_governance/compliance/report.py:345).
        # NOTE: audit.keys._resolve_uri tries base64 FIRST, then falls back
        # to raw utf-8 on failure. A 64-char hex string is coincidentally
        # valid base64 (64 chars = 48 bytes) so the b64 decode "succeeds"
        # with wrong bytes and the fingerprint check fails silently. Force
        # the env vars to be unambiguous base64 of the real key material.
        import base64 as _b64

        from codeatelier_governance.audit.keys import fingerprint_key as _fp_of

        outgoing_fp = _fp_of(outgoing_secret)
        incoming_fp = _fp_of(incoming_secret)
        os.environ["VIDEO1_OUT_KEY"] = _b64.b64encode(outgoing_secret).decode("ascii")
        os.environ["VIDEO1_IN_KEY"] = _b64.b64encode(incoming_secret).decode("ascii")
        os.environ[f"GOVERNANCE_CHAIN_KEY_{outgoing_fp[:16]}"] = "env://VIDEO1_OUT_KEY"
        os.environ[f"GOVERNANCE_CHAIN_KEY_{incoming_fp[:16]}"] = "env://VIDEO1_IN_KEY"

        # Phase 7 (halt presence marker) happens after the halt audit row
        # so the audit event's chain_seq is captured but the agent status
        # flip is already in place when verify_chain reads presence.
        await phase_halt_contract_drafter(database_url)

    await phase_verify_and_report(
        sdk2, database_url, marker_seq, pending_request_id,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="seed.py",
        description="Seed a Postgres DB with the Video 1 demo state.",
    )
    parser.add_argument(
        "--database-url", type=str, required=True,
        help="PostgreSQL DSN (postgresql://... or postgresql+asyncpg://...)",
    )
    parser.add_argument(
        "--audit-secret", type=str, required=True,
        help="64-hex-char HMAC audit secret used for the initial chain key.",
    )
    args = parser.parse_args(argv)

    try:
        asyncio.run(_run(args.database_url, args.audit_secret))
    finally:
        _restore_real_clock()


if __name__ == "__main__":
    main()
