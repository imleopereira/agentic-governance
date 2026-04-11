# Code Atelier Governance SDK

**Enforcement gates for AI agents — in-process, just Postgres.**

[![tests](https://github.com/imleopereira/code-atelier-governance/actions/workflows/test.yml/badge.svg)](https://github.com/imleopereira/code-atelier-governance/actions/workflows/test.yml)

Most LLM tools tell you what your agent did, after the fact. Code Atelier
Governance gates decisions *before* the LLM call fires. Budget caps, scope
checks, human-in-the-loop approvals, loop detection, behavioral contracts,
and a tamper-evident audit trail — all from one `pip install`, all writing to
the Postgres your application already has.

```python
import os
from codeatelier_governance import GovernanceSDK, ScopePolicy, BudgetPolicy

async with GovernanceSDK(database_url=os.environ["DATABASE_URL"]) as sdk:
    sdk.scope.register(ScopePolicy(
        agent_id="billing-agent",
        allowed_tools=frozenset({"read_invoice", "send_email"}),
        hidden_tools=frozenset({"delete_all_data"}),
    ))
    sdk.cost.register(BudgetPolicy(
        agent_id="billing-agent",
        per_session_usd=0.50,
        per_agent_usd_daily=10.00,
        per_session_seconds=300,
    ))

    await sdk.scope.check("billing-agent", tool="read_invoice")  # PASS
    await sdk.cost.check_or_raise("billing-agent", session_id)   # PASS or BudgetExceeded
    await sdk.cost.track_usage("billing-agent", session_id,
        model="gpt-4o", input_tokens=1000, output_tokens=500)    # auto-USD from pricing table
```

## Install

```bash
pip install codeatelier-governance                     # core SDK
pip install "codeatelier-governance[console]"          # + governance console GUI
pip install "codeatelier-governance[openai]"           # + OpenAI wrapper
pip install "codeatelier-governance[anthropic]"        # + Anthropic wrapper
pip install "codeatelier-governance[langchain]"        # + LangChain handler
pip install "codeatelier-governance[otel]"             # + OpenTelemetry export
```

## Setup

```bash
# Apply DDL to your Postgres
governance migrate --database-url postgresql://user:pass@host/db

# Create a console user
governance console add-user --username admin --role admin
```

## Eight enforcement modules

| Module | What it does |
|--------|-------------|
| **Audit** | HMAC-chained, append-only, tamper-evident event log with step-level provenance |
| **Scope** | Whitelist tools and APIs per agent. Hidden tools removed from agent context. Default deny. |
| **Cost** | Token + USD caps per session/day. Session time limits. Built-in pricing for 24 models. |
| **Gates** | Human-in-the-loop approval with HMAC-signed single-use tokens |
| **Loop Detection** | Sliding window detection of repeated tool calls. Auto-kill runaway agents. |
| **Presence** | Live/idle/unresponsive agent heartbeat tracking |
| **Contracts** | Pre/post conditions on tool calls. Built-in checks: hitl_approved, budget_available, scope_allowed. |
| **Compliance** | Auto-generate EU AI Act Article 12 reports from the audit trail |

## Framework adapters

```python
# OpenAI — 1 line
from codeatelier_governance.integrations.openai_wrap import wrap_openai
client = wrap_openai(OpenAI(), sdk=sdk, agent_id="my-agent")

# Anthropic — 1 line
from codeatelier_governance.integrations.anthropic_wrap import wrap_anthropic
client = wrap_anthropic(Anthropic(), sdk=sdk, agent_id="my-agent")

# LangChain — 1 line
from codeatelier_governance.integrations.langchain_handler import GovernanceCallbackHandler
handler = GovernanceCallbackHandler(sdk=sdk, agent_id="my-agent", enforce=True)
```

## Governance Console

A web dashboard for posture overview, event exploration, cost monitoring,
gate approvals, and user management. Ships as a FastAPI backend + Next.js frontend.

```bash
# Start the console
GOVERNANCE_DATABASE_URL=postgresql://... uvicorn codeatelier_governance.console.app:app
```

## CLI

```bash
governance migrate     # Apply DDL to Postgres
governance verify      # Walk HMAC chain, exit 0 (clean) or 1 (tampered)
governance tail        # Live-follow audit events
governance budget      # Show cost snapshot for an agent
governance report      # Generate EU AI Act Article 12 compliance report
governance console     # User management (add-user, list-users, disable-user, reset-password)
```

## Resilience contract

**Observation surfaces never break the host call.** `sdk.audit.log()`,
`sdk.cost.track()`, and `sdk.gates.request()` log a warning and continue
if storage is unreachable.

**Enforcement surfaces fail closed by default.** `sdk.cost.check_or_raise()`,
`sdk.scope.check()`, and `sdk.gates.wait_for()` raise by contract. On storage
failure, the cost gate denies the call rather than allowing it.

## Just Postgres

The only infrastructure dependency is a Postgres connection string. No ClickHouse,
no Redis, no Kafka, no sidecar, no background worker. We use the database your
application already has.

## Security

- HMAC-SHA256 chain on every audit event (tamper-evident)
- 13-point security checklist on every feature
- PBKDF2-HMAC-SHA256 password hashing (600k iterations)
- Pydantic strict models with size caps throughout
- Login rate limiting (5 attempts/IP/60s)
- Constant-time token comparison
- All SQL parameterized (zero injection vectors)

## Standards alignment

- **EU AI Act Article 12** (binding 2026-08-02) — automatic event logging with tamper-evidence
- **NIST CAISI AI Agent Standards** (Feb 2026) — audit reconstructability
- **OWASP Top 10 for Agentic Applications 2026** — scope enforcement, least-agency
- **SOC 2 Type II** — append-only, immutable logging patterns

## Documentation

Full documentation, quickstart guide, API reference, and concepts:

**[codeatelier.tech/governance](https://www.codeatelier.tech/governance)**

## License

MIT
