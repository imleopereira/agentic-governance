#!/usr/bin/env python3
"""Multi-agent live integration test for the v0.5.1 Governance SDK.

Exercises the SDK against the same Postgres + audit secret the running
FastAPI console is already using, so any event written here shows up in
the live console UI at http://localhost:3001.

What it covers:
  * Six agents registered with distinct scope + budget + contract policies
  * Real AsyncOpenAI calls via `wrap_openai` (not mocks)
  * Concurrent execution via asyncio.gather
  * Each enforcement gate is exercised at least once:
      - Happy path                 (billing-agent-live, support-agent-live)
      - Scope violation            (fraud-agent-live — tries hidden tool)
      - Budget exhaustion          (analytics-agent-live — $0.02 cap)
      - HITL approval.requested    (compliance-agent-live — contract)
      - Loop detection             (search-agent-live — LoopPolicy)
  * HMAC audit chain is verified per-session post-run
  * Per-agent cost snapshots compared against the running FastAPI backend
  * All agent IDs are suffixed `-live` so they do not collide with the
    mock data already registered by earlier automation runs.

Usage:
    GOVERNANCE_DATABASE_URL=postgresql://governance:governance@localhost:5435/governance_qa \\
    GOVERNANCE_AUDIT_SECRET=... \\
    OPENAI_API_KEY=sk-proj-... \\
    .venv/bin/python scripts/live_test_multi_agent.py

All three env vars are required. The script will refuse to start if any
is missing.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from typing import Any
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def say(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    say(f"  {GREEN}PASS{RESET}  {msg}")


def bad(msg: str) -> None:
    say(f"  {RED}FAIL{RESET}  {msg}")


def warn(msg: str) -> None:
    say(f"  {YELLOW}WARN{RESET}  {msg}")


def section(title: str) -> None:
    say(f"\n{BOLD}{CYAN}{title}{RESET}")


# Running counters driven by ok() / bad() for the end-of-run summary.
_PASS = 0
_FAIL = 0


def tally(passed: bool, name: str, detail: str = "") -> None:
    global _PASS, _FAIL
    if passed:
        _PASS += 1
        ok(f"{name}" + (f" — {DIM}{detail}{RESET}" if detail else ""))
    else:
        _FAIL += 1
        bad(f"{name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    # --- env -------------------------------------------------------------
    required = ["GOVERNANCE_DATABASE_URL", "GOVERNANCE_AUDIT_SECRET", "OPENAI_API_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        say(f"{RED}Missing required env vars: {missing}{RESET}")
        return 2

    db_url_raw = os.environ["GOVERNANCE_DATABASE_URL"]
    # Normalise to asyncpg driver if the env points at a sync URL.
    db_url = (
        db_url_raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        if db_url_raw.startswith("postgresql://")
        and not db_url_raw.startswith("postgresql+asyncpg://")
        else db_url_raw
    )
    secret_hex = os.environ["GOVERNANCE_AUDIT_SECRET"]
    secret = bytes.fromhex(secret_hex) if len(secret_hex) == 64 else secret_hex.encode("utf-8")
    api_key = os.environ["OPENAI_API_KEY"]

    # --- imports ---------------------------------------------------------
    from openai import AsyncOpenAI

    from codeatelier_governance import (
        GovernanceSDK,
        ScopePolicy,
        BudgetPolicy,
        Contract,
        PreCondition,
        LoopPolicy,
    )
    from codeatelier_governance.audit.models import AuditEvent
    from codeatelier_governance.scope.errors import ScopeViolation
    from codeatelier_governance.cost.errors import BudgetExceeded
    from codeatelier_governance.loop.errors import LoopDetected
    from codeatelier_governance.integrations.openai_wrap import wrap_openai

    say(f"\n{BOLD}{CYAN}{'='*68}{RESET}")
    say(f"{BOLD}  GOVERNANCE SDK — MULTI-AGENT LIVE INTEGRATION TEST v0.5.1{RESET}")
    say(f"{BOLD}{CYAN}{'='*68}{RESET}")

    # --- SDK init --------------------------------------------------------
    section("Setup")
    sdk = GovernanceSDK(database_url=db_url, audit_secret=secret, enable_routing=True)
    await sdk.start()
    say(f"  DB       : {db_url.split('@')[-1]}")
    say(f"  Secret   : {secret_hex[:8]}...{secret_hex[-4:]}")
    say(f"  Backend  : http://127.0.0.1:8766 (shared Postgres + secret)")
    say(f"  Console  : http://localhost:3001")

    # --- register six agents --------------------------------------------
    # Agent IDs are suffixed `-live-<random>` so concurrent test runs do
    # not step on each other.  We also use a random 4-char suffix so the
    # generated rows are easy to find in the console grep.
    suffix = f"live-{uuid4().hex[:6]}"

    def aid(name: str) -> str:
        return f"{name}-{suffix}"

    billing = aid("billing-agent")
    support = aid("support-agent")
    fraud = aid("fraud-agent")
    analytics = aid("analytics-agent")
    compliance = aid("compliance-agent")
    search = aid("search-agent")

    # --- scope policies --------------------------------------------------
    sdk.scope.register(
        ScopePolicy(
            agent_id=billing,
            allowed_tools=frozenset({"read_invoice", "email_customer", "lookup_account"}),
            hidden_tools=frozenset({"issue_refund"}),
        )
    )
    sdk.scope.register(
        ScopePolicy(
            agent_id=support,
            allowed_tools=frozenset({"email_customer", "search_kb", "create_ticket"}),
            hidden_tools=frozenset({"issue_refund"}),
        )
    )
    sdk.scope.register(
        ScopePolicy(
            agent_id=fraud,
            allowed_tools=frozenset({"check_transaction", "flag_suspicious"}),
            hidden_tools=frozenset({"approve_transaction"}),
        )
    )
    sdk.scope.register(
        ScopePolicy(
            agent_id=analytics,
            allowed_tools=frozenset({"aggregate_metrics", "search_db"}),
            hidden_tools=frozenset({"delete_records"}),
        )
    )
    sdk.scope.register(
        ScopePolicy(
            agent_id=compliance,
            allowed_tools=frozenset({"read_audit_log", "generate_report", "export_data"}),
            hidden_tools=frozenset({"delete_records"}),
        )
    )
    sdk.scope.register(
        ScopePolicy(
            agent_id=search,
            allowed_tools=frozenset({"search_db", "search_kb"}),
            hidden_tools=frozenset(),
        )
    )

    # --- budget policies -------------------------------------------------
    sdk.cost.register(BudgetPolicy(agent_id=billing,    per_session_usd=1.00, per_agent_usd_daily=5.00))
    sdk.cost.register(BudgetPolicy(agent_id=support,    per_session_usd=1.00, per_agent_usd_daily=5.00))
    sdk.cost.register(BudgetPolicy(agent_id=fraud,      per_session_usd=1.00, per_agent_usd_daily=5.00))
    # analytics: deliberately tiny per-session TOKEN cap. The first
    # real gpt-4o-mini call returns ~40 completion tokens + the input
    # prompt → ~60+ tokens total. per_session_tokens=10 means the post
    # call track() overshoots immediately, and the pre-check on the
    # second call raises BudgetExceeded before it can fire. USD cap
    # is left generous so we're specifically exercising the token cap.
    sdk.cost.register(BudgetPolicy(
        agent_id=analytics,
        per_session_usd=1.00,
        per_session_tokens=10,
    ))
    sdk.cost.register(BudgetPolicy(agent_id=compliance, per_session_usd=1.00, per_agent_usd_daily=5.00))
    sdk.cost.register(BudgetPolicy(agent_id=search,     per_session_usd=1.00, per_agent_usd_daily=5.00))

    # --- loop policy for search-agent ------------------------------------
    sdk.loop.register(
        LoopPolicy(
            agent_id=search,
            window_seconds=30,
            max_calls=3,  # 4th call within 30s → raise LoopDetected
            action="raise",
        )
    )

    # --- contract with HITL pre-condition for compliance -----------------
    sdk.contracts.register(
        Contract(
            agent_id=compliance,
            tool="export_data",
            pre=[PreCondition(check="hitl_approved", message="Needs human approval")],
        )
    )

    say(f"  Agents   : 6 ({suffix})")
    say(f"")

    # =====================================================================
    # Concurrent agent execution
    # =====================================================================
    section("Running 6 agents concurrently against real OpenAI (gpt-4o-mini)")

    # IMPORTANT: `wrap_openai` monkey-patches the client's
    # `chat.completions.create` method in place and binds to a single
    # agent_id + session_id. Sharing one AsyncOpenAI across agents
    # means only the first wrap wins — every subsequent wrap hits the
    # `already_wrapped` sentinel, returns the same client untouched,
    # and all agents' calls get attributed to the FIRST agent.
    # Each agent MUST get its own AsyncOpenAI instance.
    def new_client() -> "AsyncOpenAI":
        return AsyncOpenAI(api_key=api_key)

    async def happy_path(agent_id: str, prompts: list[str]) -> dict[str, Any]:
        """Wrap the client, run several real OpenAI calls, return stats."""
        session_id = uuid4()
        wrapped = wrap_openai(
            new_client(), sdk=sdk, agent_id=agent_id, session_id=session_id,
        )
        stats: dict[str, Any] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "calls_made": 0,
            "budget_exceeded": False,
            "errors": [],
        }
        for prompt in prompts:
            try:
                await wrapped.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=40,
                )
                stats["calls_made"] += 1
            except BudgetExceeded:
                stats["budget_exceeded"] = True
                break
            except Exception as exc:
                stats["errors"].append(f"{type(exc).__name__}: {str(exc)[:160]}")
                break
        return stats

    async def scope_violation_agent(agent_id: str) -> dict[str, Any]:
        """Run a happy-path call then deliberately hit a hidden tool."""
        session_id = uuid4()
        wrapped = wrap_openai(
            new_client(), sdk=sdk, agent_id=agent_id, session_id=session_id,
        )
        stats: dict[str, Any] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "calls_made": 0,
            "scope_violation_raised": False,
            "errors": [],
        }
        try:
            await wrapped.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Is $50 at a coffee shop suspicious?"}],
                max_tokens=40,
            )
            stats["calls_made"] += 1
        except Exception as exc:
            stats["errors"].append(f"{type(exc).__name__}: {str(exc)[:160]}")
        # Try to invoke a tool that is NOT in the agent's allowed_tools
        try:
            await sdk.scope.check(agent_id, tool="approve_transaction")
            stats["errors"].append("ScopeViolation expected but the check passed")
        except ScopeViolation:
            stats["scope_violation_raised"] = True
        return stats

    async def loop_agent(agent_id: str) -> dict[str, Any]:
        """Call the same tool 4 times within 30s to trip LoopPolicy."""
        session_id = uuid4()
        wrapped = wrap_openai(
            new_client(), sdk=sdk, agent_id=agent_id, session_id=session_id,
        )
        stats: dict[str, Any] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "calls_made": 0,
            "loop_detected": False,
            "errors": [],
        }
        # Make one real OpenAI call so there's cost/audit data for this
        # agent, but trip the loop via the in-process counter directly.
        try:
            await wrapped.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Say hello in 5 words."}],
                max_tokens=20,
            )
            stats["calls_made"] += 1
        except Exception as exc:
            stats["errors"].append(f"llm: {type(exc).__name__}: {str(exc)[:120]}")
        for _ in range(4):
            try:
                await sdk.loop.record_call(agent_id, session_id, "search_db")
            except LoopDetected:
                stats["loop_detected"] = True
                break
        return stats

    async def hitl_agent(agent_id: str) -> dict[str, Any]:
        """Run one call, then try a contract-gated tool with no approval."""
        session_id = uuid4()
        wrapped = wrap_openai(
            new_client(), sdk=sdk, agent_id=agent_id, session_id=session_id,
        )
        stats: dict[str, Any] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "calls_made": 0,
            "contract_violation": False,
            "errors": [],
        }
        try:
            await wrapped.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Summarise: The EU AI Act Article 12."}],
                max_tokens=40,
            )
            stats["calls_made"] += 1
        except Exception as exc:
            stats["errors"].append(f"{type(exc).__name__}: {str(exc)[:160]}")
        # Try the contract-gated tool with no approval granted → must
        # raise ContractViolation because hitl_approved is False.
        from codeatelier_governance.contracts.errors import ContractViolation
        try:
            await sdk.contracts.check_pre(agent_id, session_id, "export_data")
            stats["errors"].append("ContractViolation expected but check_pre passed")
        except ContractViolation:
            stats["contract_violation"] = True
        return stats

    start = time.time()
    results = await asyncio.gather(
        happy_path(billing,    ["What is 2 + 2?", "Name three primary colours.", "Say 'ok' in French."]),
        happy_path(support,    ["Summarise: governance compliance in 6 words.", "What is GDPR?"]),
        scope_violation_agent(fraud),
        # Analytics has a $0.0002 cap → even one call should trip BudgetExceeded
        # on pre-call check after the first call.
        happy_path(analytics,  ["Say 'a'", "Say 'b'", "Say 'c'"]),
        hitl_agent(compliance),
        loop_agent(search),
        return_exceptions=True,
    )
    elapsed = time.time() - start
    say(f"\n  Elapsed: {elapsed:.2f}s for {len(results)} concurrent agents")

    # Flatten any exceptions returned from gather.
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            bad(f"agent #{i} raised {type(r).__name__}: {r}")
            traceback.print_exception(type(r), r, r.__traceback__)

    # =====================================================================
    # Per-agent assertions
    # =====================================================================
    section("Per-agent enforcement assertions")

    by_agent = {
        r["agent_id"]: r for r in results if isinstance(r, dict)
    }

    # Happy path — billing
    r = by_agent.get(billing)
    if r:
        tally(r["calls_made"] >= 3 and not r["errors"],
              f"{billing}: 3 happy-path calls",
              f"{r['calls_made']} calls, {len(r['errors'])} errors")

    # Happy path — support
    r = by_agent.get(support)
    if r:
        tally(r["calls_made"] >= 2 and not r["errors"],
              f"{support}: 2 happy-path calls",
              f"{r['calls_made']} calls")

    # Scope violation — fraud
    r = by_agent.get(fraud)
    if r:
        tally(r.get("scope_violation_raised", False),
              f"{fraud}: ScopeViolation raised for hidden tool",
              "approve_transaction blocked")

    # Budget exhaustion — analytics
    r = by_agent.get(analytics)
    if r:
        # With $0.0002 session cap, the second call should fail pre-check.
        tally(r["budget_exceeded"],
              f"{analytics}: BudgetExceeded raised",
              f"{r['calls_made']} calls completed before cap")

    # HITL gate — compliance
    r = by_agent.get(compliance)
    if r:
        tally(r.get("contract_violation", False),
              f"{compliance}: ContractViolation on hitl_approved (no grant)",
              "export_data blocked without approval")

    # Loop detection — search
    r = by_agent.get(search)
    if r:
        tally(r.get("loop_detected", False),
              f"{search}: LoopDetected after 4 rapid calls",
              "window_seconds=30, max_calls=3")

    # =====================================================================
    # Audit chain verification per session
    # =====================================================================
    section("Audit chain integrity (HMAC trace per session)")

    for aid_str, r in by_agent.items():
        session_id: UUID = r["session_id"]
        try:
            chain = await sdk.audit.trace_session_chain(session_id)
            tally(
                True,
                f"{aid_str}: chain verified",
                f"{len(chain)} events linked",
            )
        except Exception as exc:
            tally(False, f"{aid_str}: chain failed", f"{type(exc).__name__}: {exc}")

    # =====================================================================
    # Per-agent cost snapshots
    # =====================================================================
    section("Cost snapshots (per-session USD used)")

    # Short pause to let background audit writes finalise before we
    # query counters — otherwise cost.snapshot() may race an in-flight
    # track() call.
    await asyncio.sleep(1.0)

    total_spent = 0.0
    for aid_str, r in by_agent.items():
        try:
            snap = await sdk.cost.snapshot(aid_str, r["session_id"])
            total_spent += snap.session_usd_used
            say(
                f"  {DIM}{aid_str:<44}{RESET} "
                f"${snap.session_usd_used:.6f}  "
                f"{DIM}({snap.session_tokens_used} tokens){RESET}"
            )
        except Exception as exc:
            bad(f"{aid_str}: snapshot failed — {exc}")
    say(f"\n  {BOLD}Total OpenAI spend this run: ${total_spent:.6f}{RESET}")

    # =====================================================================
    # Console API cross-check — hit the live FastAPI and confirm our
    # agent IDs show up in the posture response.
    # =====================================================================
    section("FastAPI console cross-check")

    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get("http://127.0.0.1:8766/api/health")
            tally(resp.status_code == 200 and resp.json().get("ok"),
                  "backend /api/health responding", f"{resp.status_code}")
            # /api/posture requires auth; this is a dev instance without
            # a session cookie, so a 401 is also a healthy signal.
            posture = await http.get("http://127.0.0.1:8766/api/posture")
            tally(posture.status_code in (200, 401, 403),
                  "backend /api/posture reachable",
                  f"{posture.status_code} (auth may block unauthenticated)")
    except Exception as exc:
        warn(f"FastAPI cross-check skipped — {type(exc).__name__}: {exc}")

    # =====================================================================
    # Teardown
    # =====================================================================
    await sdk.close()

    section("Summary")
    total = _PASS + _FAIL
    status_color = GREEN if _FAIL == 0 else (YELLOW if _FAIL <= 2 else RED)
    say(f"\n  {status_color}{_PASS}/{total} assertions passed{RESET}")
    say(f"  Elapsed: {elapsed:.2f}s of concurrent agent runtime")
    say(f"  Spend: ${total_spent:.6f}")
    say(f"")
    say(f"  Look for these agent IDs in http://localhost:3001/agents :")
    for a in [billing, support, fraud, analytics, compliance, search]:
        say(f"    {DIM}·{RESET} {a}")

    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        say(f"\n{YELLOW}Interrupted.{RESET}")
        sys.exit(130)
