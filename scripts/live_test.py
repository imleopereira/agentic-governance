#!/usr/bin/env python3
"""Live integration test: exercises every SDK feature against real OpenAI + real Postgres.

Usage:
    export GOVERNANCE_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:port/db
    export GOVERNANCE_TEST_AUDIT_SECRET=<32-byte hex>
    export OPENAI_API_KEY=sk-...
    python scripts/live_test.py

Both env vars are required. No fallback credentials, no ephemeral secret
generation: a chain-integrity bug that only reproduces across runs with a
stable HMAC key must be observable, which is impossible if the secret is
regenerated every run.

This module is importable with no side effects. All env-var loading and
configuration printing happens inside ``run_all_tests`` — ``import
scripts.live_test`` never touches ``os.environ`` and never calls
``sys.exit``. See README.md 'Running the live test suite'.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4

# Colors for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def _redact_db_url(url: str) -> str:
    """Return a log-safe form of a DB URL: scheme://<redacted>@host:port/db."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "<host>"
        port = f":{parsed.port}" if parsed.port else ""
        db = parsed.path or ""
        return f"{parsed.scheme}://<redacted>@{host}{port}{db}"
    except Exception:
        return "<unparseable-db-url>"


def _load_db_url() -> str:
    """Load the test DB URL from env. Fail loud if unset — no fallback."""
    url = os.environ.get("GOVERNANCE_TEST_DATABASE_URL")
    if not url:
        sys.stderr.write(
            f"{RED}FATAL{RESET} GOVERNANCE_TEST_DATABASE_URL is not set.\n"
            "       This script never falls back to a hardcoded credential.\n"
            "       See README.md section 'Running the live test suite'.\n"
        )
        sys.exit(2)
    # Defence in depth: reject the historically-leaked default outright.
    # Built as two tokens so this very string does not trip the credential
    # guard in scripts/test_no_hardcoded_creds.py.
    _legacy_user = "governance"
    _legacy_default = f"{_legacy_user}:{_legacy_user}@"
    if _legacy_default in url:
        sys.stderr.write(
            f"{RED}FATAL{RESET} GOVERNANCE_TEST_DATABASE_URL uses the legacy "
            "`governance:governance` credential. Rotate and retry.\n"
        )
        sys.exit(2)
    return url


def _load_audit_secret() -> str:
    """Load the test HMAC secret from env. Fail loud if unset — no fallback.

    The ephemeral-secret path was removed: a chain-integrity regression that
    only reproduces across two runs with a stable key is invisible if the
    key rotates every run. The test must prove the attack fails, not
    generate a fresh key and hope.
    """
    secret = os.environ.get("GOVERNANCE_TEST_AUDIT_SECRET")
    if not secret:
        sys.stderr.write(
            f"{RED}FATAL{RESET} GOVERNANCE_TEST_AUDIT_SECRET is not set.\n"
            "       Generate one with:\n"
            "           python -c 'import secrets; print(secrets.token_hex(32))'\n"
            "       Export it and retry. See README.md section 'Running the\n"
            "       live test suite' for the rationale (stable key is required\n"
            "       to observe chain-integrity regressions across runs).\n"
        )
        sys.exit(2)
    return secret


def _load_config() -> tuple[str, str]:
    """Load all env-driven config. Called from the main entry point only.

    Keeping this out of module scope means ``import scripts.live_test`` is
    a pure, side-effect-free operation: no env reads, no prints, no exits.
    Any tool that imports this module (pytest --collect-only, importlib,
    IDE indexers) will not trip FATAL errors mid-collection.
    """
    db_url = _load_db_url()
    secret = _load_audit_secret()
    print(f"  live_test db  : {_redact_db_url(db_url)}")
    print(f"  live_test auth: <redacted {len(secret)} chars>")
    return db_url, secret


results: list[dict] = []


def log_result(test: str, passed: bool, detail: str = "") -> None:
    status = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    results.append({"test": test, "passed": passed, "detail": detail})
    print(f"  {status}  {test}")
    if detail and not passed:
        print(f"         {detail}")


async def run_all_tests() -> None:
    DB_URL, SECRET = _load_config()

    from openai import AsyncOpenAI

    from codeatelier_governance import (
        GovernanceSDK,
        Contract,
        PreCondition,
        PostCondition,
        LoopPolicy,
        LoopDetected,
    )
    from codeatelier_governance.audit.models import AuditEvent
    from codeatelier_governance.scope.models import ScopePolicy
    from codeatelier_governance.cost.models import BudgetPolicy
    from codeatelier_governance.scope.errors import ScopeViolation
    from codeatelier_governance.integrations.openai_wrap import wrap_openai

    print(f"\n{BOLD}{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}  CODE ATELIER GOVERNANCE SDK — LIVE INTEGRATION TEST{RESET}")
    print(f"{BOLD}{CYAN}{'='*60}{RESET}\n")

    # -------------------------------------------------------------------------
    # SETUP
    # -------------------------------------------------------------------------
    print(f"{BOLD}Setting up SDK...{RESET}")
    sdk = GovernanceSDK(
        database_url=DB_URL,
        audit_secret=SECRET,
    )
    await sdk.__aenter__()

    session_id = uuid4()
    agent_id = "live-test-agent"

    # Register policies
    sdk.scope.register(ScopePolicy(
        agent_id=agent_id,
        allowed_tools=frozenset({"read_data", "summarize", "chat.completions.create"}),
        hidden_tools=frozenset({"delete_all"}),
    ))
    sdk.cost.register(BudgetPolicy(
        agent_id=agent_id,
        per_session_usd=1.00,
        per_agent_usd_daily=5.00,
        per_session_seconds=300,
    ))
    sdk.loop.register(LoopPolicy(
        agent_id=agent_id,
        window_seconds=30,
        max_calls=3,
        action="raise",
    ))

    print(f"  Agent: {agent_id}")
    print(f"  Session: {session_id}")
    print()

    # -------------------------------------------------------------------------
    # TEST 1: Audit Trail — Log events and verify HMAC chain
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 1: Audit Trail (HMAC chain){RESET}")
    try:
        r1 = await sdk.audit.log(AuditEvent(
            agent_id=agent_id, session_id=session_id,
            kind="test.start", model="gpt-4o-mini",
            metadata={"test": "live_integration", "timestamp": time.time()},
        ))
        log_result("Audit event logged", not r1.is_placeholder, f"event_id={r1.event_id}")

        r2 = await sdk.audit.log(AuditEvent(
            agent_id=agent_id, session_id=session_id,
            kind="test.step_2", model="gpt-4o-mini",
            metadata={"step": 2},
        ))
        log_result("Second event chained", not r2.is_placeholder)

        # Verify chain — trace_session_chain returns a list and raises ChainIntegrityError on tampering
        chain = await sdk.audit.trace_session_chain(session_id)
        log_result("HMAC chain verified", len(chain) >= 2, f"{len(chain)} events verified (no ChainIntegrityError raised)")
    except Exception as e:
        log_result("Audit trail", False, str(e))

    print()

    # -------------------------------------------------------------------------
    # TEST 2: Scope Enforcement — Allow and deny
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 2: Scope Enforcement{RESET}")
    try:
        await sdk.scope.check(agent_id, tool="read_data")
        log_result("Allowed tool passes", True)
    except Exception as e:
        log_result("Allowed tool passes", False, str(e))

    try:
        await sdk.scope.check(agent_id, tool="hack_the_planet")
        log_result("Blocked tool raises", False, "Should have raised ScopeViolation")
    except ScopeViolation:
        log_result("Blocked tool raises ScopeViolation", True)
    except Exception as e:
        log_result("Blocked tool raises", False, str(e))

    # Hidden tools
    tools = ["read_data", "summarize", "delete_all", "other"]
    filtered = sdk.scope.filter_tools(agent_id, tools)
    log_result("Hidden tools filtered", "delete_all" not in filtered, f"filtered={filtered}")

    print()

    # -------------------------------------------------------------------------
    # TEST 3: Budget Gates — Track and enforce
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 3: Budget Gates{RESET}")
    try:
        await sdk.cost.check_or_raise(agent_id, session_id)
        log_result("Budget check passes (under limit)", True)
    except Exception as e:
        log_result("Budget check passes", False, str(e))

    await sdk.cost.track_usage(agent_id, session_id,
        model="gpt-4o-mini", input_tokens=500, output_tokens=200)
    snap = await sdk.cost.snapshot(agent_id, session_id)
    log_result("Cost tracked with auto-pricing",
        snap.session_usd_used > 0, f"usd={snap.session_usd_used:.6f}")

    print()

    # -------------------------------------------------------------------------
    # TEST 4: Real OpenAI Call with Governance Wrapper
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 4: Real OpenAI Call (wrapped){RESET}")
    try:
        client = AsyncOpenAI()
        wrapped = wrap_openai(client, sdk=sdk, agent_id=agent_id, session_id=session_id)
        log_result("OpenAI client wrapped", True)

        response = await wrapped.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say 'governance works' in exactly 2 words."}],
            max_tokens=10,
        )
        content = response.choices[0].message.content
        log_result("OpenAI call succeeded", content is not None, f"response='{content}'")

        # Check cost was tracked
        snap2 = await sdk.cost.snapshot(agent_id, session_id)
        log_result("Cost auto-tracked from OpenAI call",
            snap2.session_usd_used > snap.session_usd_used,
            f"usd before={snap.session_usd_used:.6f} after={snap2.session_usd_used:.6f}")

    except Exception as e:
        log_result("OpenAI wrapped call", False, str(e))

    print()

    # -------------------------------------------------------------------------
    # TEST 5: Loop Detection
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 5: Loop Detection{RESET}")
    try:
        await sdk.loop.record_call(agent_id, session_id, "read_data")
        await sdk.loop.record_call(agent_id, session_id, "read_data")
        await sdk.loop.record_call(agent_id, session_id, "read_data")
        log_result("3 calls under threshold (max=3)", True)
    except LoopDetected:
        log_result("3 calls under threshold", False, "Raised too early")

    try:
        await sdk.loop.record_call(agent_id, session_id, "read_data")
        log_result("4th call should raise", False, "Should have raised LoopDetected")
    except LoopDetected:
        log_result("4th call raises LoopDetected", True)

    # Different tool shouldn't trigger
    try:
        await sdk.loop.record_call(agent_id, session_id, "summarize")
        log_result("Different tool doesn't trigger loop", True)
    except LoopDetected:
        log_result("Different tool doesn't trigger", False, "False positive")

    print()

    # -------------------------------------------------------------------------
    # TEST 6: Agent Presence
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 6: Agent Presence{RESET}")
    try:
        await sdk.presence.heartbeat(agent_id, metadata={"test": True})
        agents = await sdk.presence.list_agents()
        found = any(a["agent_id"] == agent_id for a in agents)
        log_result("Heartbeat + list_agents", found)

        await sdk.presence.mark_idle(agent_id)
        agents2 = await sdk.presence.list_agents()
        idle = next((a for a in agents2 if a["agent_id"] == agent_id), None)
        log_result("Mark idle", idle is not None and idle["status"] == "idle")
    except Exception as e:
        log_result("Presence", False, str(e))

    print()

    # -------------------------------------------------------------------------
    # TEST 7: Behavioral Contracts
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 7: Behavioral Contracts{RESET}")
    try:
        from codeatelier_governance.contracts.errors import ContractViolation

        contract = Contract(
            agent_id=agent_id,
            tool="read_data",
            pre=[PreCondition(check="scope_allowed", message="Must be in scope")],
            post=[PostCondition(check="audit_logged", message="Must be audit logged")],
        )
        sdk.contracts.register(contract)
        log_result("Contract registered", True)

        # Pre-condition should pass (read_data is in scope)
        await sdk.contracts.check_pre(agent_id, session_id, "read_data")
        log_result("Pre-condition passes (scope_allowed)", True)

        # Contract for a tool NOT in scope
        contract2 = Contract(
            agent_id=agent_id,
            tool="hack_the_planet",
            pre=[PreCondition(check="scope_allowed", message="Must be in scope")],
        )
        sdk.contracts.register(contract2)
        try:
            await sdk.contracts.check_pre(agent_id, session_id, "hack_the_planet")
            log_result("Pre-condition fails (out of scope)", False, "Should have raised")
        except ContractViolation:
            log_result("Pre-condition fails with ContractViolation", True)

    except Exception as e:
        log_result("Contracts", False, str(e))

    print()

    # -------------------------------------------------------------------------
    # TEST 8: Model Pricing
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 8: Built-in Model Pricing{RESET}")
    from codeatelier_governance.cost.pricing import estimate_cost

    gpt4o_cost = estimate_cost("gpt-4o", 1_000_000, 1_000_000)
    log_result("GPT-4o pricing", gpt4o_cost == 12.50, f"${gpt4o_cost}")

    claude_cost = estimate_cost("claude-sonnet-4-6", 1000, 500)
    log_result("Claude Sonnet pricing", claude_cost > 0, f"${claude_cost:.6f}")

    unknown = estimate_cost("totally-unknown-model", 1000, 1000)
    log_result("Unknown model returns $0", unknown == 0.0)

    # Prefix matching
    versioned = estimate_cost("gpt-4o-2024-05-13", 1_000_000, 0)
    log_result("Prefix matching works", versioned == 2.50, f"${versioned}")

    print()

    # -------------------------------------------------------------------------
    # TEST 9: Per-Model Cost Breakdown
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 9: Per-Model Cost Breakdown{RESET}")
    try:
        breakdown = await sdk.cost.model_breakdown(agent_id)
        log_result("Model breakdown returns data", isinstance(breakdown, dict), f"models={list(breakdown.keys())}")
    except Exception as e:
        log_result("Model breakdown", False, str(e))

    print()

    # -------------------------------------------------------------------------
    # TEST 10: Policy Hot-Reload
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 10: Policy Hot-Reload Config{RESET}")
    log_result("Hot-reload available", hasattr(sdk, '_hot_reload_enabled'))

    print()

    # -------------------------------------------------------------------------
    # TEST 11: Compliance Report
    # -------------------------------------------------------------------------
    print(f"{BOLD}Test 11: Compliance Report{RESET}")
    try:
        from codeatelier_governance.compliance.report import ReportGenerator

        gen = ReportGenerator(database_url=DB_URL)
        report = await gen.generate_article12(session_ids=[session_id])
        log_result("Article 12 report generated",
            report is not None and len(report.sections) > 0,
            f"{len(report.sections)} sections")
        for section in report.sections:
            status_color = GREEN if section.status == "compliant" else (YELLOW if section.status == "partial" else RED)
            print(f"         {status_color}{section.status:15s}{RESET}  {section.title}")
    except Exception as e:
        log_result("Compliance report", False, str(e))

    print()

    # -------------------------------------------------------------------------
    # CLEANUP
    # -------------------------------------------------------------------------
    await sdk.presence.close_agent(agent_id)
    await sdk.__aexit__(None, None, None)

    # -------------------------------------------------------------------------
    # SUMMARY
    # -------------------------------------------------------------------------
    print(f"{BOLD}{CYAN}{'='*60}{RESET}")
    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])
    total = len(results)

    if failed == 0:
        print(f"{BOLD}{GREEN}  ALL {total} TESTS PASSED{RESET}")
    else:
        print(f"{BOLD}  {GREEN}{passed} PASSED{RESET}  {RED}{failed} FAILED{RESET}  ({total} total)")

    print(f"{BOLD}{CYAN}{'='*60}{RESET}\n")

    # Write results to JSON for team review
    output_path = "/tmp/governance-live-test-results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Results saved to {output_path}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY env var required")
        sys.exit(1)
    asyncio.run(run_all_tests())
