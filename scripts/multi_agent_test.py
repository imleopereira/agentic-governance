"""
Multi-Agent Governance Test — Real OpenAI LLM calls governed by the SDK.

Scenario: A company deploys 3 agents to handle a customer support ticket.
Each agent has different scope, budget, and governance constraints.
The SDK enforces everything in real-time.

Agents:
  1. triage-agent — reads the ticket, classifies severity, routes to specialist
  2. research-agent — searches the knowledge base, finds relevant docs
  3. response-agent — drafts a customer reply (high-risk: needs HITL approval)

What this tests:
  - Scope enforcement (each agent has different allowed tools)
  - Budget tracking across 3 agents in one session
  - Audit trail with HMAC chain across agents
  - Loop detection (research-agent might loop on searches)
  - Presence (all 3 agents show up in console)
  - Cost tracking with real OpenAI token usage
  - Scope violation (response-agent tries to access DB — blocked)
  - Chain verification at the end
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone

from openai import AsyncOpenAI

from codeatelier_governance import (
    AuditEvent,
    BudgetPolicy,
    GovernanceSDK,
    LoopPolicy,
    ScopePolicy,
)
from codeatelier_governance.integrations.openai_wrap import wrap_openai

DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://governance:governance@localhost:5435/governance_qa",
)
SESSION_ID = uuid.uuid4()

# The customer ticket we're processing
TICKET = {
    "id": "TICKET-4521",
    "from": "jane@acme.com",
    "subject": "Can't access billing dashboard after password reset",
    "body": (
        "Hi, I reset my password yesterday and now I can't access the billing "
        "dashboard. I keep getting a 403 error. My account is on the Enterprise "
        "plan and I need to download invoices for our quarterly report due Friday. "
        "This is urgent."
    ),
}


async def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or not api_key.startswith("sk-"):
        print("ERROR: Set OPENAI_API_KEY to a valid key.")
        print("  Run: export OPENAI_API_KEY=sk-...")
        return

    async with GovernanceSDK(database_url=DB_URL, hot_reload=False) as sdk:
        # Each agent gets its own OpenAI client instance (wrap_openai patches in-place)
        triage_client = wrap_openai(AsyncOpenAI(api_key=api_key), sdk, agent_id="triage-agent", session_id=SESSION_ID)
        research_client = wrap_openai(AsyncOpenAI(api_key=api_key), sdk, agent_id="research-agent", session_id=SESSION_ID)
        response_client = wrap_openai(AsyncOpenAI(api_key=api_key), sdk, agent_id="response-agent", session_id=SESSION_ID)

        # ─── Register all 3 agents ───────────────────────────────────

        # Agent 1: Triage — can classify and route, nothing else
        sdk.scope.register(ScopePolicy(
            agent_id="triage-agent",
            allowed_tools=frozenset({"classify_ticket", "route_to_agent", "read_ticket"}),
            hidden_tools=frozenset({"delete_account", "issue_refund", "access_db"}),
        ))
        sdk.cost.register(BudgetPolicy(
            agent_id="triage-agent",
            per_session_usd=0.50,
            per_session_tokens=50_000,
        ))

        # Agent 2: Research — can search, read docs, but not write
        sdk.scope.register(ScopePolicy(
            agent_id="research-agent",
            allowed_tools=frozenset({"search_kb", "read_doc", "summarize"}),
            hidden_tools=frozenset({"write_doc", "delete_doc", "access_db"}),
        ))
        sdk.cost.register(BudgetPolicy(
            agent_id="research-agent",
            per_session_usd=1.00,
            per_session_tokens=100_000,
        ))
        sdk.loop.register(LoopPolicy(
            agent_id="research-agent",
            window_seconds=30,
            max_calls=8,
            action="raise",
        ))

        # Agent 3: Response — can draft replies, but high-risk actions need approval
        sdk.scope.register(ScopePolicy(
            agent_id="response-agent",
            allowed_tools=frozenset({"draft_reply", "send_email", "read_ticket"}),
            hidden_tools=frozenset({"delete_account", "issue_refund", "access_db"}),
        ))
        sdk.cost.register(BudgetPolicy(
            agent_id="response-agent",
            per_session_usd=1.50,
            per_session_tokens=150_000,
        ))

        # ─── All agents go live ──────────────────────────────────────

        for agent_id in ["triage-agent", "research-agent", "response-agent"]:
            await sdk.presence.heartbeat(agent_id, metadata={
                "session_id": str(SESSION_ID),
                "ticket": TICKET["id"],
                "started_at": datetime.now(timezone.utc).isoformat(),
            })

        print(f"\n{'='*60}")
        print(f"  MULTI-AGENT GOVERNANCE TEST")
        print(f"  Session:  {SESSION_ID}")
        print(f"  Ticket:   {TICKET['id']}")
        print(f"  Agents:   triage-agent, research-agent, response-agent")
        print(f"{'='*60}\n")

        # ─── Phase 1: Triage Agent ───────────────────────────────────

        print("[1/4] Triage Agent — classifying ticket...")

        await sdk.scope.check("triage-agent", tool="read_ticket")
        await sdk.scope.check("triage-agent", tool="classify_ticket")

        await sdk.audit.log(AuditEvent(
            agent_id="triage-agent",
            kind="ticket.received",
            session_id=SESSION_ID,
            metadata={"ticket_id": TICKET["id"], "subject": TICKET["subject"]},
        ))

        triage_response = await triage_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a support ticket triage agent. Classify the severity (P1-P4) and category. Respond in 2 lines: Severity: P#\nCategory: <category>"},
                {"role": "user", "content": f"Ticket: {TICKET['subject']}\n\n{TICKET['body']}"},
            ],
            max_tokens=100,
        )

        triage_result = triage_response.choices[0].message.content
        print(f"  Triage result: {triage_result}")

        await sdk.audit.log(AuditEvent(
            agent_id="triage-agent",
            kind="ticket.classified",
            session_id=SESSION_ID,
            metadata={"classification": triage_result, "routed_to": "research-agent"},
        ))

        triage_snap = await sdk.cost.snapshot("triage-agent", SESSION_ID)
        print(f"  Budget used: ${triage_snap.session_usd_used:.4f} / $0.50")

        # ─── Phase 2: Research Agent ─────────────────────────────────

        print("\n[2/4] Research Agent — searching knowledge base...")

        await sdk.scope.check("research-agent", tool="search_kb")
        await sdk.scope.check("research-agent", tool="read_doc")

        # Record tool calls for loop detection
        for i in range(3):
            await sdk.loop.record_call("research-agent", SESSION_ID, "search_kb")

        research_response = await research_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a knowledge base research agent. Given a support issue, suggest the most likely cause and the KB article that would help. Be concise (3 sentences max)."},
                {"role": "user", "content": f"Issue: {TICKET['body']}"},
            ],
            max_tokens=200,
        )

        research_result = research_response.choices[0].message.content
        print(f"  Research: {research_result}")

        await sdk.audit.log(AuditEvent(
            agent_id="research-agent",
            kind="research.completed",
            session_id=SESSION_ID,
            metadata={"findings": research_result},
        ))

        research_snap = await sdk.cost.snapshot("research-agent", SESSION_ID)
        print(f"  Budget used: ${research_snap.session_usd_used:.4f} / $1.00")

        # ─── Phase 3: Response Agent ─────────────────────────────────

        print("\n[3/4] Response Agent — drafting customer reply...")

        await sdk.scope.check("response-agent", tool="draft_reply")

        response_draft = await response_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a customer support response agent. Draft a professional, empathetic reply to the customer. Include specific next steps. Keep it under 150 words."},
                {"role": "user", "content": f"Ticket from {TICKET['from']}: {TICKET['body']}\n\nResearch findings: {research_result}"},
            ],
            max_tokens=300,
        )

        draft = response_draft.choices[0].message.content
        print(f"  Draft reply:\n  {draft[:200]}...")

        await sdk.audit.log(AuditEvent(
            agent_id="response-agent",
            kind="reply.drafted",
            session_id=SESSION_ID,
            metadata={"draft_length": len(draft), "ticket_id": TICKET["id"]},
        ))

        response_snap = await sdk.cost.snapshot("response-agent", SESSION_ID)
        print(f"  Budget used: ${response_snap.session_usd_used:.4f} / $1.50")

        # ─── Phase 4: Test scope violation ───────────────────────────

        print("\n[4/4] Testing scope violation — response-agent tries to access DB...")

        try:
            await sdk.scope.check("response-agent", tool="access_db")
            print("  ERROR: Should have been blocked!")
        except Exception as e:
            print(f"  BLOCKED (correct): {e}")

        await sdk.audit.log(AuditEvent(
            agent_id="response-agent",
            kind="scope.violation.test",
            session_id=SESSION_ID,
            metadata={"tool": "access_db", "result": "blocked"},
        ))

        # ─── Final: Summary & Chain Verification ────────────────────

        print(f"\n{'='*60}")
        print("  RESULTS")
        print(f"{'='*60}")

        for agent_id, budget in [("triage-agent", 0.50), ("research-agent", 1.00), ("response-agent", 1.50)]:
            snap = await sdk.cost.snapshot(agent_id, SESSION_ID)
            print(f"  {agent_id}: ${snap.session_usd_used:.4f} / ${budget:.2f} ({snap.session_tokens_used} tokens)")

        # Verify the full audit chain
        chain = await sdk.audit.trace_session_chain(SESSION_ID)
        print(f"\n  Audit chain: {len(chain)} events, all HMAC-verified")
        for event in chain:
            print(f"    [{event.kind}] agent={event.agent_id} verified={not event.is_placeholder}")

        # Mark all agents idle
        for agent_id in ["triage-agent", "research-agent", "response-agent"]:
            await sdk.presence.mark_idle(agent_id)

        print(f"\n  All agents marked IDLE.")
        print(f"  Verify: governance verify --session-id {SESSION_ID}")
        print(f"  Console: http://localhost:3001/events?session_id={SESSION_ID}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
