# User Story — A Day in the Life of a Governance SDK User

This document tells the story of one developer adopting Code Atelier Governance
in a real production codebase. It is written from the developer's point of view
and walks through the first 30 days of usage.

The Governance SDK ships as a pip-installable Python package. v0.1 implements
six features (decision audit trail, step-level provenance, prompt versioning,
action scope enforcement, spend limits, and human-in-the-loop gates). This story
focuses on the **audit + provenance** module — the foundation everything else
builds on.

---

## The user

Maya is a backend engineer at a Series A health-tech startup. Her team built
an LLM-powered agent that updates patient records. Compliance just told her:
"this can't ship to production until we have an immutable audit trail of every
action the agent takes."

Maya's stack:
- Python 3.11 backend
- Postgres (managed, RDS)
- FastAPI for the API layer
- The agent itself talks to OpenAI directly

She has one afternoon to prove to compliance that the agent can be trusted.

---

## Minute 0 — discovery

Maya searches "python ai agent audit trail postgres" and lands on
`codeatelier-governance`. The README opens with a four-line example. She reads
to the end in under 60 seconds and decides to try it.

## Minute 1 — install

```bash
pip install codeatelier-governance
export GOVERNANCE_AUDIT_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')
```

She stores the secret in her team's existing secrets manager — same place all
her other env vars live. No new infrastructure.

## Minute 2 — schema

```bash
psql "$DATABASE_URL" -f $(python -c \
  'import codeatelier_governance.audit, os; \
   print(os.path.join(os.path.dirname(codeatelier_governance.audit.__file__), "ddl.sql"))')
```

This creates one table — `governance_audit_events` — plus the database-level
triggers that make it append-only. She reads the comments at the bottom of the
DDL and runs the recommended `REVOKE UPDATE, DELETE, TRUNCATE` against her
application's database role for defense in depth. Now even her own application
cannot modify or delete an audit row.

## Minute 3 — wire it up

```python
from codeatelier_governance import GovernanceSDK

sdk = GovernanceSDK(database_url=os.environ["DATABASE_URL"])

async def main():
    await sdk.start()
    try:
        # ... her existing app code ...
    finally:
        await sdk.close()
```

Three lines to instantiate, one start/close pair. She wires `start()` into her
FastAPI lifespan handler so it runs once at app boot.

## Minute 4 — first audit row

Maya wraps her agent's risky function with the `@sdk.audit.track` decorator:

```python
from codeatelier_governance.audit import AuditEvent

@sdk.audit.track(kind="patient.update", agent_id="record-editor-v2")
async def update_patient(patient_id: str, fields: dict) -> dict:
    return await db.patients.update(patient_id, fields)
```

She runs her test suite. She queries Postgres:

```sql
SELECT kind, agent_id, created_at
FROM governance_audit_events
ORDER BY created_at;
```

```
         kind          |     agent_id     |          created_at
-----------------------+------------------+-------------------------------
 patient.update.start  | record-editor-v2 | 2026-04-09 23:14:02.118+00
 patient.update.end    | record-editor-v2 | 2026-04-09 23:14:02.241+00
```

Two rows: a start event with the input hash, an end event with the output hash.
They link to each other through `parent_event_id`.

**Time from `pip install` to first audit log: under five minutes.** Maya posts
a screenshot in #engineering and goes to lunch.

---

## Day 3 — the first weird incident

A patient record changed and nobody on the team knows why. The on-call engineer
pings Maya. She grabs the suspicious row's `event_id` from the audit table:

```python
chain = await sdk.audit.trace(event_id)
for record in chain:
    print(f"{record.created_at}  {record.kind:25}  {record.agent_id}")
    print(f"  metadata: {record.metadata}")
```

Output:

```
2026-04-12 14:02:11  agent.start                record-editor-v2
  metadata: {'session_user': 'maya@health.co'}
2026-04-12 14:02:11  llm.call                   record-editor-v2
  metadata: {'model': 'claude-sonnet-4-6'}
2026-04-12 14:02:13  llm.result                 record-editor-v2
  metadata: {'tool_calls': 1}
2026-04-12 14:02:13  patient.update.start       record-editor-v2
  metadata: {}
2026-04-12 14:02:13  patient.update.end         record-editor-v2
  metadata: {}
```

`trace()` walked the `parent_event_id` chain from the leaf back to the root,
returning every event that led to the suspicious one — *and verified the HMAC
of every row along the way*. Maya finds the answer in seconds: the agent
received a tool call result it interpreted as authorization to update the
record. She fixes the prompt, ships the fix, and writes the post-mortem with
the trace output pasted in as primary evidence.

She didn't grep application logs. She didn't `tail -f`. She didn't reconstruct
state from timestamps. The audit chain told her what happened, in order, with
cryptographic guarantees that nothing in between was missing.

---

## Day 7 — the security review

Compliance asks the question Maya was waiting for: *"Could a malicious insider
with database write access tamper with these logs without detection?"*

Maya runs the SDK's exploit test suite:

```bash
pytest tests/audit/test_exploits.py -v
```

```
test_attacker_cannot_silently_modify_a_stored_row PASSED
test_oversize_metadata_is_rejected_at_validation_time PASSED
test_oversize_agent_id_is_rejected PASSED
test_short_secret_is_rejected PASSED
test_writer_never_silently_drops_events PASSED
test_chain_built_with_wrong_secret_fails_verification PASSED
```

Each test states an attacker scenario and proves the control holds. Six attacks,
six green checks. The compliance officer signs off. The proof is executable, not
a slide deck.

The detail that closed the deal: every audit row stores an HMAC over its own
immutable fields *plus the previous row's HMAC*. Modifying any byte of any past
row breaks that row's HMAC and every row that follows. `trace()` raises
`ChainIntegrityError` the moment it detects the break. Compliance can run the
verification on demand against the live database without taking the system
offline.

---

## Day 14 — the database blip

Their managed Postgres has a 30-second blip during a maintenance window. Maya
braces for an incident — and nothing happens. The application keeps serving
requests. She checks the structured logs after the fact:

```
audit.primary_write_failed_degraded events_buffered=87
audit.primary_write_failed_degraded events_buffered=412
audit.primary_recovered events_flushed=412
```

Zero events lost. Zero requests failed. Maya checks her monitoring dashboard:
no spike in 5xx, no spike in latency, no alerts. The SDK held writes in an
in-memory fallback buffer the whole time and replayed every event on the first
successful flush. The application never saw the outage.

Maya files this fact in her head as the most underrated thing the SDK does:
**governance never blocks the host application, even when the governance
backend is down.**

---

## Day 30 — concurrency stress

Her team scales the agent to 50 concurrent invocations on the same session.
Maya worries the HMAC chain will get scrambled by races: two log calls happen
at the same time, both read the same `prev_hash`, both compute their chain
link from the same predecessor, and one of them gets overwritten.

She doesn't have to test it. The SDK's existing test suite already proves it:
`test_concurrent_logs_keep_chain_monotonic` fires 50 concurrent log calls in a
single session and asserts that exactly one event has a null `prev_hash` (the
first), and every other event's `prev_hash` matches some other event's HMAC in
the result set. The chain is monotonic under load because chain construction is
serialized by a per-session asyncio lock — and only by session, so different
sessions never block each other.

She turns up the dial to 500 concurrent calls. It still works.

---

## Month 2 — adding the enforcement modules

By the end of month one, Maya's audit trail is producing 200,000 rows a day
across six agents in production. The compliance story is solid. But she now
has three new problems audit alone can't solve:

1. The agent can call **any** Python function its developer wires up. Last
   week one of the LLM hallucinated a tool name that happened to match an
   internal admin function. Nothing bad happened — but nothing stopped it,
   either.
2. Finance pinged her about a $312 OpenAI bill from a single overnight batch.
   The audit trail showed exactly *what* happened (a retry loop), but didn't
   *prevent* it.
3. Compliance wants a human in the loop before any agent updates a patient
   record marked "high-risk." Right now the agent just does it.

The Governance SDK ships three more enforcement modules in the same package
that solve all three. Maya adds them in one afternoon.

### Module 2 — Action Scope Enforcement

```python
from codeatelier_governance.scope import ScopePolicy

sdk.scope.register(ScopePolicy(
    agent_id="record-editor-v2",
    allowed_tools=frozenset({"read_patient", "update_patient_field", "send_email"}),
    allowed_apis=frozenset({
        "GET https://api.internal.health.co/v1/patients/*",
        "PATCH https://api.internal.health.co/v1/patients/*",
    }),
))

@sdk.scope.require_tool("update_patient_field", agent_id="record-editor-v2")
async def update_patient_field(patient_id: str, field: str, value: str) -> None:
    ...
```

The whitelist is exact-match (or explicit prefix with a trailing `*`). No
regex, no glob, no eval — Maya read the source and confirmed there's no way
a maliciously-crafted tool name can slip through. The decorator runs the
check before the function body. If the agent tries to call a tool that's not
in the whitelist, it raises `ScopeViolation` *and* writes a `scope.violation`
audit row that compliance can grep for.

She tests it by manually calling a forbidden tool from a Python REPL:

```python
>>> await update_patient_field(...)  # ok
>>> await call_internal_admin_function(...)
codeatelier_governance.scope.errors.ScopeViolation: scope violation:
  tool='call_internal_admin_function' is not in scope for agent='record-editor-v2'
```

The next morning she greps the audit table for `kind = 'scope.violation'`
and finds three attempts overnight where the LLM tried to call functions
that don't exist. None of them ran. None of them produced output. They all
got logged.

### Module 3 — Spend Limits & Budget Gates

```python
from codeatelier_governance.cost import BudgetPolicy, BudgetExceeded

sdk.cost.register(BudgetPolicy(
    agent_id="record-editor-v2",
    per_session_usd=0.25,
    per_session_tokens=20_000,
    per_agent_usd_daily=10.00,
))

# Inside the agent loop:
async def call_llm(prompt: str, session_id: UUID) -> str:
    try:
        await sdk.cost.check_or_raise("record-editor-v2", session_id)
    except BudgetExceeded as exc:
        # The audit row is already written. Surface to caller.
        raise HTTPException(429, str(exc))
    
    response = await openai.chat.completions.create(...)
    
    await sdk.cost.track(
        "record-editor-v2",
        session_id,
        tokens=response.usage.total_tokens,
        usd=calculate_cost(response),
    )
    return response.choices[0].message.content
```

She picks 25 cents per session and ten dollars per day per agent. Two days
later the same retry loop happens — and stops itself at the eleven-cent mark
when the per-session cap is exceeded. The audit table shows a
`budget.exceeded` row; the agent's HTTP handler returned a 429 to the
caller; finance never got paged.

The counter is monotonic — there's no `refund()` method and `track()`
rejects negative deltas. The agent can't decrement its own counter to
re-enter a budget it already exhausted. Maya verified this in the exploit
test suite (`test_track_rejects_negative_delta`).

### Module 4 — Human-in-the-Loop Gates

```python
from codeatelier_governance.gates import ApprovalDenied, ApprovalTimeout

@sdk.gates.require_approval(
    kind="patient.high_risk_update",
    agent_id="record-editor-v2",
    timeout=600,  # ten minutes for a human to respond
)
async def high_risk_update(patient_id: str, fields: dict) -> None:
    # Function body only runs after a human grants approval.
    await db.patients.update(patient_id, fields)
```

When the agent calls `high_risk_update("p-883", {...})`, four things happen:

1. The decorator opens an approval request and writes an
   `approval.requested` audit row.
2. The decorator blocks the call, waiting up to ten minutes for resolution.
3. Maya's existing on-call workflow (a Slack bot that watches the audit
   table for new approval requests) posts a button to the #compliance
   channel. The button calls `sdk.gates.grant(token)` or `.deny(token)`.
4. When the human clicks Grant, the audit row `approval.granted` is
   written, the decorator unblocks, and the function body runs.

If the human denies, `ApprovalDenied` is raised. If nobody clicks within
ten minutes, `ApprovalTimeout` is raised. Either way the audit row tells
the story.

The Slack bot doesn't get to forge approvals. The token in the audit row
is a 64-byte HMAC bound to the request_id, the action_hash, and the
expiration timestamp. The HMAC key lives in the same env var as the audit
secret — anyone trying to forge an approval would also be able to forge
audit rows, which is the harder attack we already defended against in
month one.

Maya tries to grant the same token twice (simulating a Slack double-click)
and gets `ApprovalTokenError: token already used (single-use only)`. She
tries to swap the action_hash to grant a different patient update with the
same token and gets `ApprovalTokenError: action_hash mismatch`. Both
attempts get logged as audit events.

### Five lines per module, one substrate

She notices the pattern: every module is one import, one `register()` call
at startup, and one decorator (or function call) inside the agent's loop.
Every enforcement decision writes an audit row to the same chain. There's
one audit secret, one Postgres table, one connection string. The four
modules are not four products glued together — they share the substrate.

Compliance signs off on the high-risk update flow on the same day she
ships it because the proof is in the audit chain: every approval, every
grant, every denial, every budget cap that fired, every scope violation
that blocked an action — all of it sits in one Postgres table that the
auditors can `SELECT *` from with their existing read-only role.

---

## What Maya doesn't have yet (v0.1 limitations)

After the month-2 enforcement modules ship, this is what's left:

| Module | Status | What it will do |
|---|---|---|
| `audit` | **shipped** | Decision audit trail + step-level provenance with HMAC chain |
| `scope` | **shipped** | Whitelist tools and APIs the agent is allowed to call |
| `cost` | **shipped** | Token and dollar caps per agent / session / day |
| `gates` | **shipped** | Human-in-the-loop approval gates with signed tokens |
| `prompts` | not yet | Hash-keyed prompt versioning with diff and rollback |
| `auth` | stub | Agent identity (full identity is v0.2; v0.1 ships `agent_id: str`) |

Things Maya also doesn't get yet:
- A `governance migrate` CLI (she runs `psql -f ddl.sql` manually). v0.1.6.
- Pre-built LangChain CallbackHandler / OpenAI SDK adapter (she uses the
  decorator pattern instead — works fine, just one extra line per call). v0.1.6.
- A web dashboard (she queries Postgres directly with SQL — and if she wants
  charts, she points Grafana at the same database)
- Managed-mode hosting (everything self-hosted in v0.1)
- Auto-cleanup for resolved gate rows (she runs the `cleanup_resolved` helper
  on a cron, or `DELETE WHERE resolved_at < NOW() - INTERVAL '90 days'`)

---

## What Maya gets that other tools don't

Maya evaluated LangSmith, Arize, Langfuse, and Helicone before choosing
`codeatelier-governance`. The differences that mattered to her:

1. **Append-only at three layers.** Application-layer frozen models, database
   triggers, and a REVOKE recipe for the SDK's database role. Most observability
   tools log to ClickHouse where any row can be deleted by anyone with write
   access.
2. **Tamper-evident HMAC chain.** Verifiable from the database alone without
   trusting the application. Compliance can run the verification themselves.
3. **Zero new infrastructure.** It writes to her existing Postgres. No
   ClickHouse, no Kafka, no second database to back up.
4. **Host app keeps working when governance is down.** She didn't have to
   build a circuit breaker around the SDK. The SDK has one built in.
5. **Five-line integration with sane defaults.** Not "configure your YAML" or
   "wire up our middleware to your framework". One class, one connection
   string.

These five properties are non-negotiable invariants of the SDK, not
configuration options to enable. They are what the word "governance" means in
this product.

---

## Closing

Compliance signed off on day 7. The agent went to production on day 10.
By the end of month two, Maya's team had layered scope enforcement, spend
limits, and human-in-the-loop gates on top of the audit trail — adding each
module in roughly the time it takes to write a unit test, and wiring all
four enforcement points into the same Postgres table. Six agents in
production, hundreds of thousands of audit rows per day, zero tampered
records, zero overnight bills, three runtime denials of tools the LLM
hallucinated into existence. Compliance, finance, and engineering all read
the same table.

Maya spent an afternoon on the initial integration, an afternoon on the
month-two extension, and the rest of her time on her actual job.
