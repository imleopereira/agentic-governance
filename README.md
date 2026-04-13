# Code Atelier Governance SDK

**Enforcement gates for every action routed through the SDK — in-process, just Postgres.**

[![tests](https://github.com/imleopereira/code-atelier-governance/actions/workflows/test.yml/badge.svg)](https://github.com/imleopereira/code-atelier-governance/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/code-atelier-governance)](https://pypi.org/project/code-atelier-governance/)

Most LLM tools tell you what your agent did, after the fact. Code Atelier
Governance gates decisions *before* the LLM call fires — for every action
routed through the SDK, not just tracing after the fact. Budget caps, scope
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
| **Audit** | HMAC-chained, append-only event log with step-level provenance and chain fork detection. Each entry is cryptographically linked to the previous entry at write time. Chain integrity can be verified on-demand via `sdk.audit.verify_chain()` or by enabling `verify_chain_on_read=True`. |
| **Scope** | Whitelist tools and APIs per agent. Hidden tools removed from agent context. Default deny. |
| **Cost** | Token + USD caps per session/day. Session time limits. Built-in pricing for 25+ models. Combined budget query for low-latency enforcement. Requires `max_tokens` to be declared on each call. |
| **Gates** | Human-in-the-loop approval with HMAC-signed single-use tokens. Self-approval prevention (fail-closed). |
| **Loop Detection** | Sliding window detection of repeated tool calls. Auto-halt runaway agents. |
| **Presence** | Live/idle/unresponsive/halted agent heartbeat tracking with operator identity. |
| **Contracts** | Pre/post conditions on tool calls. Built-in checks: hitl_approved, budget_available, scope_allowed. |
| **Compliance** | Generates the event log required by EU AI Act Article 12 for all actions routed through the SDK. Produces an Article 12 evidence report from the audit trail. The report does not assert compliance — it provides evidence for actions the SDK observed. Article 12 compliance for your deployment depends on routing all relevant AI actions through the SDK. |

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
governance report      # Generate EU AI Act Article 12 evidence report for actions the SDK observed
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

- HMAC-SHA256 chain on every audit event (fork-detecting; chain integrity verified on-demand or on each read)
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

- **EU AI Act Article 12** (binding 2026-08-02) — generates the automatic event log required by Article 12 for all actions routed through the SDK. Compliance for your deployment depends on routing all relevant AI actions through the SDK.
- **NIST CAISI AI Agent Standards** (Feb 2026) — audit reconstructability
- **OWASP Top 10 for Agentic Applications 2026** — scope enforcement, least-agency
- **SOC 2 Type II** — append-only, immutable logging patterns

## Threat Model

This section addresses what the SDK protects against and where it does not
provide protection. Deployers and security reviewers should read this before
treating the SDK as a complete security boundary.

### What the SDK protects against

The following are blocked in-process, before the LLM call fires:

- Accidental tool or API calls that violate a registered scope policy — blocked by `sdk.scope.check()` before the call is made.
- Session or per-agent budget overruns — blocked by `sdk.cost.check_or_raise()` when projected usage would exceed the configured limit.
- High-risk actions without human approval — blocked by HITL gates when `blocking=True`; the gate raises `ApprovalRequired` until a reviewer resolves the request.
- Audit log tampering — detected via HMAC chain verification, available on-demand via `sdk.audit.verify_chain()` or on each read with `verify_chain_on_read=True`.

### What the SDK does NOT protect against

- **Direct client bypass.** Any code path that calls `anthropic.Anthropic()` or `openai.OpenAI()` directly, without going through `wrap_anthropic()` or `wrap_openai()`, is invisible to all SDK gates. Budget, scope, and audit logging are all bypassed. An LLM-generated tool function that instantiates its own client is not governed.
- **Process-level bypass.** The SDK provides in-process enforcement gates. It does not provide kernel-level, network-level, or process-isolation-level enforcement. A second Python process or subprocess that bypasses the SDK wrappers entirely is not governed.
- **Streaming cost precision.** Streaming calls are budget-gated using the declared `max_tokens` value before the stream opens. Actual token usage is recorded from the stream's final usage object. If the LLM API does not return a usage object in the stream, the SDK falls back to `max_tokens` as the tracked value — actual usage may differ.
- **On-demand tampering detection only.** The HMAC audit chain detects tampering when verification is explicitly run (`sdk.audit.verify_chain()`) or on each read (`verify_chain_on_read=True`). It does not alert on tampering as it occurs, and does not prevent deletion of the entire chain by a privileged database administrator who can restart the process with a new HMAC key.
- **HITL non-blocking mode.** When a HITL gate is configured with `blocking=False`, the gate raises `ApprovalPending` and the caller is responsible for not proceeding. The SDK cannot prevent a caller who ignores `ApprovalPending` from proceeding anyway.

### Deployment guidance

- Route ALL LLM client instances through the SDK wrappers (`wrap_openai()`, `wrap_anthropic()`). The SDK's startup warning will flag if no wrappers are registered.
- For network-level enforcement that blocks all outbound LLM calls regardless of SDK usage, use an API gateway or proxy in front of your LLM providers.
- For Article 12 compliance evidence, the SDK logs all actions it observes. A deployment where some LLM calls bypass the wrapper will produce an incomplete evidence record.

## Documentation

Full documentation, quickstart guide, API reference, and concepts:

**[codeatelier.tech/governance](https://www.codeatelier.tech/governance)**

## License

MIT
