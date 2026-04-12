# Code Atelier Governance SDK

**Enforcement gates for AI agents — in-process, just Postgres.**

[![tests](https://github.com/imleopereira/code-atelier-governance/actions/workflows/test.yml/badge.svg)](https://github.com/imleopereira/code-atelier-governance/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/code-atelier-governance)](https://pypi.org/project/code-atelier-governance/)

Most LLM tools tell you what your agent did, after the fact. Code Atelier
Governance gates decisions *before* the LLM call fires. Budget caps, scope
checks, human-in-the-loop approvals, loop detection, behavioral contracts,
and a tamper-evident audit trail — all from one `pip install`, all writing to
the Postgres your application already has.

```python
from codeatelier_governance import GovernanceSDK, ScopePolicy, BudgetPolicy, AuditEvent
import uuid

async with GovernanceSDK(database_url="postgresql://...") as sdk:
    sdk.scope.register(ScopePolicy(
        agent_id="billing-agent",
        allowed_tools=frozenset({"read_invoice", "send_email"}),
    ))
    sdk.cost.register(BudgetPolicy(
        agent_id="billing-agent", per_session_usd=5.00,
    ))

    await sdk.scope.check("billing-agent", tool="read_invoice")       # PASS
    await sdk.cost.check_or_raise("billing-agent", session_id)        # PASS or BudgetExceeded
    await sdk.audit.log(AuditEvent(
        agent_id="billing-agent", kind="invoice.read", session_id=session_id,
    ))
```

### Sync support (Flask / Django)

```python
from codeatelier_governance import GovernanceSDKSync

with GovernanceSDKSync(database_url="postgresql://...") as sdk:
    sdk.scope.check("my-agent", tool="send_email")
    sdk.cost.check_or_raise("my-agent", session_id)
```

## Install

```bash
pip install code-atelier-governance                      # core SDK
pip install "code-atelier-governance[console]"            # + governance console GUI
pip install "code-atelier-governance[openai]"             # + OpenAI wrapper
pip install "code-atelier-governance[anthropic]"          # + Anthropic wrapper
pip install "code-atelier-governance[langchain]"          # + LangChain handler
pip install "code-atelier-governance[otel]"               # + OpenTelemetry export
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
| **Audit** | HMAC-chained, append-only, tamper-evident event log with step-level provenance and chain fork detection |
| **Scope** | Whitelist tools and APIs per agent. Hidden tools removed from agent context. Default deny. |
| **Cost** | Token + USD caps per session/day. Session time limits. Built-in pricing for 25+ models. Combined budget query for low-latency enforcement. |
| **Gates** | Human-in-the-loop approval with HMAC-signed single-use tokens. Self-approval prevention (fail-closed). |
| **Loop Detection** | Sliding window detection of repeated tool calls. Auto-halt runaway agents. |
| **Presence** | Live/idle/unresponsive/halted agent heartbeat tracking with operator identity. |
| **Contracts** | Pre/post conditions on tool calls. Built-in checks: hitl_approved, budget_available, scope_allowed. |
| **Compliance** | Auto-generate EU AI Act Article 12 reports from the audit trail |

## Framework adapters

```python
# OpenAI — 1 line (async and sync clients supported)
from codeatelier_governance.integrations.openai_wrap import wrap_openai
client = wrap_openai(AsyncOpenAI(), sdk=sdk, agent_id="my-agent")

# Anthropic — 1 line
from codeatelier_governance.integrations.anthropic_wrap import wrap_anthropic
client = wrap_anthropic(AsyncAnthropic(), sdk=sdk, agent_id="my-agent")

# LangChain — 1 line
from codeatelier_governance.integrations.langchain_handler import GovernanceCallbackHandler
handler = GovernanceCallbackHandler(sdk=sdk, agent_id="my-agent", enforce=True)
```

## Governance Console

A web dashboard with real-time SSE event streaming, agent topology view,
HITL approval queue, cost monitoring, and chain verification. Ships as a
FastAPI backend + Next.js frontend.

```bash
# Start the console backend
GOVERNANCE_DATABASE_URL=postgresql://... python -m codeatelier_governance.console

# Start the frontend (dev)
cd console && npm run dev
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

## Performance

- **Shared connection pool**: single engine, ~15 connections per SDK instance
- **Concurrent audit writes**: pre-call audit backgrounded, post-call ops parallelized
- **Combined budget query**: session + daily counters in one DB round-trip
- **Serverless ready**: policies loaded on start(), no 30s cold-start gap

## Resilience contract

**Observation surfaces never break the host call.** `sdk.audit.log()`,
`sdk.cost.track()`, and `sdk.gates.request()` log a warning and continue
if storage is unreachable. Graceful JSONL fallback on read-only filesystems.

**Enforcement surfaces fail closed by default.** `sdk.cost.check_or_raise()`,
`sdk.scope.check()`, and `sdk.gates.wait_for()` raise by contract. On storage
failure, the cost gate denies the call rather than allowing it.

## Just Postgres

The only infrastructure dependency is a Postgres connection string. No ClickHouse,
no Redis, no Kafka, no sidecar, no background worker. We use the database your
application already has.

## Security

- HMAC-SHA256 chain on every audit event (tamper-evident, fork-detecting)
- Self-approval prevention on HITL gates (fail-closed)
- 13-point security checklist on every feature
- PBKDF2-HMAC-SHA256 password hashing (600k iterations)
- Pydantic strict models with size caps throughout
- Login rate limiting (5 attempts/IP/60s)
- Constant-time token comparison
- All SQL parameterized (zero injection vectors)
- Error messages sanitized (no DB URLs, SQL, or internal paths leak)
- Weak audit secret detection (entropy check)

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
