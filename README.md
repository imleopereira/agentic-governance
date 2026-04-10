# Code Atelier Governance SDK

> **v0.1.5 — Multi-worker correct, single-region, single-database.**
> Audit chain, cost counters, and HITL gates are all correct under
> multi-process deployments behind a single Postgres. The cost gate
> defaults to **fail-closed**: if the cost store is unreachable, the
> call is denied (set ``cost_fail_open=True`` for availability over
> safety). Audit events that can't reach Postgres spill to a local
> JSONL file that survives process restarts and auto-drains on recovery.
> 22 adversarial security scenarios are tested in
> ``qa/security_red_team.py`` against real Postgres + real uvicorn
> workers and all currently pass.

**Enforcement gates for AI agents — five lines, in-process, just Postgres.**

Most LLM observability tools tell you what your agent did, after the fact.
Code Atelier Governance is different: it gates decisions *before* the LLM call
fires. Budget caps, scope checks, human-in-the-loop approvals, and a
tamper-evident audit trail — all from one `pip install`, all writing to the
Postgres your application already has, all opt-in via decorators.

```python
from codeatelier_governance import GovernanceSDK
from codeatelier_governance.scope import ScopePolicy
from codeatelier_governance.cost import BudgetPolicy

sdk = GovernanceSDK(database_url=os.environ["DATABASE_URL"])
sdk.scope.register(ScopePolicy(
    agent_id="billing-agent",
    allowed_tools=frozenset({"read_invoice", "send_email"}),
))
sdk.cost.register(BudgetPolicy(
    agent_id="billing-agent",
    per_session_usd=0.50,
    per_agent_usd_daily=10.00,
))

@sdk.scope.require_tool("read_invoice", agent_id="billing-agent")
@sdk.audit.track(kind="invoice.fetch", agent_id="billing-agent")
async def read_invoice(invoice_id: str) -> dict:
    ...
```

That's an in-process scope check, an in-process budget check, and a
tamper-evident audit row written to your Postgres. No proxy. No sidecar.
No second database to back up.

## Resilience contract (v0.1.5)

**Observation surfaces never break the host call.** ``sdk.audit.log()``,
``sdk.cost.track()``, and ``sdk.gates.request()`` will log a structured
warning and continue if our internal storage is on fire — your
application's request keeps moving. ``audit.log()`` returns a placeholder
record (with ``record.is_placeholder == True``) so you can detect the
degraded state if you care to alert on it.

**Enforcement surfaces fail closed by default.** ``sdk.cost.check_or_raise()``,
``sdk.scope.check()``, and ``sdk.gates.wait_for()`` raise by contract.
On internal storage failure, the cost gate denies the call rather than
allowing it — an attacker who takes down the cost store cannot drain
budgets. Pass ``cost_fail_open=True`` if you'd rather have availability
than safety; the failure is audit-logged either way.

**Tamper-evident at the database level.** Every audit row stores an
HMAC-SHA256 over its immutable fields plus the previous row's HMAC.
Tampering with any past row breaks every subsequent row's verification.
``sdk.audit.trace_session_chain(session_id)`` re-verifies the entire
session chain on demand and raises ``ChainIntegrityError`` at the first
broken row.

## Why "enforcement gates" and not "tracing"

| Tool | What it does |
|---|---|
| LangSmith / Langfuse / Arize / Braintrust | Observability — log what happened, after the fact |
| Helicone | Proxy that logs requests |
| Portkey / LiteLLM | Gateway that routes traffic |
| **Code Atelier Governance** | **In-process SDK that BLOCKS bad actions before they execute, and writes a tamper-evident audit log of every decision** |

We are the only pip-installable SDK that:

- Gates decisions **before** the LLM call fires (not just after)
- Writes to your **own Postgres**, not a vendor's ClickHouse
- Couples **audit + scope + cost + HITL** as one substrate, not four products
- Ships an **HMAC-chained, append-only audit log** that's tamper-evident at the database level
- Keeps your application running when the **governance database is unreachable**

## Features (v0.1)

| Module | Status | What it does |
|---|---|---|
| `audit` | shipped | Append-only HMAC-chained log, batched writes, degraded-mode fallback |
| `scope` | shipped | Whitelist tools and APIs per agent (default deny) |
| `cost` | shipped | Token + USD caps per session and per agent / UTC day |
| `gates` | shipped | HITL approval gates with signed single-use tokens |
| `audit/otel_exporter` | shipped via `[otel]` extra | OpenTelemetry GenAI export — feed audit events into Datadog, Honeycomb, Tempo |
| `prompts` | next | Hash-keyed prompt versioning with diff and rollback |
| `auth` | stub in v0.1, real in v0.2 | Agent identity (just `agent_id: str` in v0.1) |

## Just Postgres

The only infrastructure dependency is a Postgres connection string. No
ClickHouse, no Redis, no Kafka, no S3, no sidecar process, no background
worker, no message broker. We use the database your application already has.

Optional `[otel]` extra adds OpenTelemetry export so audit events flow into
your existing observability stack — but the network egress is the host
application's `TracerProvider`, not us. Even with OTel enabled we never open
a socket of our own.

## Install

```bash
pip install codeatelier-governance               # core
pip install "codeatelier-governance[otel]"       # + OpenTelemetry export
```

## Security model

Every audit row stores an HMAC-SHA256 over its immutable fields plus the
previous row's HMAC. Modifying any byte of any past row breaks every
subsequent row's verification. Verification is constant-time and runs on
demand against the live database — your auditors can run it themselves
without taking the system offline.

The HMAC secret lives in `GOVERNANCE_AUDIT_SECRET` (env var) or your
existing secrets manager. Lose it and you can't verify your historical
audit rows. Leak it and an attacker who *also* has database write access
can forge rows. Treat it with the same care as your database password.

Every feature passes a 13-point security checklist before shipping. Every
module includes an exploit test suite that proves the controls hold.

## Standards alignment

- **EU AI Act Article 12** (binding 2026-08-02) — automatic event logging
  with six-month retention. Our HMAC chain goes beyond the legal floor by
  adding tamper-evidence.
- **NIST CAISI AI Agent Standards Initiative** (Feb 2026) — we align with
  the audit-reconstructability direction of the initiative.
- **OWASP Top 10 for Agentic Applications 2026** — scope enforcement maps
  to least-agency design principles.
- **SOC 2 Type II** — append-only logging patterns compatible with auditor
  evidence requirements.

## Read the story

`docs/user-story.md` walks through one developer's first 90 days adopting
the SDK in a real production application. Compliance signs off on day 7;
the agent goes to prod on day 10; the enforcement modules land in month two.
