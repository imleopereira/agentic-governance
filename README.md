# Agentic Governance SDK

**Enforcement gates for every action routed through the SDK — in-process, just Postgres.**

[![PyPI](https://img.shields.io/pypi/v/code-atelier-governance)](https://pypi.org/project/code-atelier-governance/)
[![Python](https://img.shields.io/pypi/pyversions/code-atelier-governance)](https://pypi.org/project/code-atelier-governance/)

Most LLM tools tell you what your agent did, after the fact. Code Atelier
Governance gates decisions *before* the LLM call fires — for every action
routed through the SDK, not just tracing after the fact. Budget caps, scope
checks, human-in-the-loop approvals, loop detection, behavioral contracts,
and a tamper-evident audit trail — all from one `pip install`, all writing to
the Postgres your application already has.

```python
from codeatelier_governance import GovernanceSDK, ScopePolicy, BudgetPolicy, AuditEvent
import uuid

session_id = uuid.uuid4()

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
import uuid

session_id = uuid.uuid4()

with GovernanceSDKSync(database_url="postgresql://...") as sdk:
    sdk.scope.check("my-agent", tool="send_email")
    sdk.cost.check_or_raise("my-agent", session_id)
```

## Install

```bash
pip install code-atelier-governance                      # core SDK
pip install "code-atelier-governance[openai]"             # + OpenAI wrapper
pip install "code-atelier-governance[anthropic]"          # + Anthropic wrapper
pip install "code-atelier-governance[langchain]"          # + LangChain handler
pip install "code-atelier-governance[otel]"               # + OpenTelemetry export
pip install "code-atelier-governance[migrations]"         # + alembic + psycopg3 (one-time, for `alembic upgrade head`)
```

The `[migrations]` extra is required for fresh installs and for
upgrading v0.5.x deployments to v0.6. `governance migrate` applies the
base DDL and then runs `alembic upgrade head` automatically, so a
single command takes a new database all the way to HEAD. See
`docs/migrations.md` for the full runbook and `docs/configuration.md`
for every environment variable the SDK reads.

## Setup

```bash
# One command: DDL + alembic upgrade head.
governance migrate --database-url postgresql://user:pass@host/db
```

## Scaffold a ready-to-run agent (v0.7.2+)

Prefer starting from a working project? The SDK ships a scaffolder that
writes a full Microsoft AGT agent wired through `scope.check`,
`cost.check_or_raise`, and `gates.request` — enforcement on from the
first line:

```bash
pip install code-atelier-governance
codeatelier-governance recipe agt ./my-agent
cd my-agent && python agent.py
```

The generated project has a refund tool where anything over $1,000
blocks on a human decision — a concrete demo of HITL gates on real
Postgres. Other recipes (`langgraph`, `crewai`) are on the v0.8
roadmap.

## Eight enforcement modules

| Module | What it does |
|--------|-------------|
| **Audit** | HMAC-chained, append-only event log with step-level provenance and chain fork detection. Each entry is cryptographically linked to the previous entry at write time. Chain integrity can be verified on-demand via `sdk.audit.verify_chain()` or by enabling `verify_chain_on_read=True`. |
| **Scope** | Whitelist tools and APIs per agent. Hidden tools removed from agent context. Default deny. |
| **Cost** | Token + USD caps per session/day. Session time limits. Built-in pricing for 25+ models. Combined budget query for low-latency enforcement. Uses `max_tokens` for forward-looking projection when declared; warns and falls back to a balance-only check otherwise. |
| **Gates** | Human-in-the-loop approval with HMAC-signed, single-use tokens bound to the request, action, and key version. |
| **Loop Detection** | Sliding-window detection of repeated tool calls; raises `LoopDetected` when configured to. |
| **Presence** | Live/idle/unresponsive/halted agent heartbeat tracking with operator identity. |
| **Contracts** | Pre/post conditions on tool calls. Built-in checks: hitl_approved, budget_available, scope_allowed. |
| **Compliance** | Generates the event log required by EU AI Act Article 12 for all actions routed through the SDK. Produces an Article 12 evidence report from the audit trail. The report does not assert compliance — it provides evidence for actions the SDK observed. Article 12 compliance for your deployment depends on routing all relevant AI actions through the SDK. |

## What's new in v0.6.2

**v0.6.2 flips several public SDK defaults to fail-closed.** If you are
upgrading from v0.5.x or v0.6.0/v0.6.1, read the "Upgrade from" sections
of `CHANGELOG.md` before `pip install -U` — most breaks surface on the
first call, not at import time.

- **BREAKING**: `CostModule.strict_unknown_models=True` default —
  unknown model names (fine-tunes, custom aliases) raise
  `UnknownModelError` instead of pricing at `$0`. Set
  `cost_strict_unknown_models=False` with a fallback rate to keep
  v0.5.x behavior.
- **BREAKING**: `InMemoryAuditStore(on_full="raise")` default — hitting
  `max_events` now raises `StoreUnavailableError` instead of silently
  evicting. Pass `on_full="evict"` for ring-buffer semantics.
- **BREAKING**: `RevocationStore(strict_chain=True)` default — when the
  audit write inside `revoke_with_chain_event` fails, the revocation
  raises instead of writing `chain_event_id=None`. Pass
  `strict_chain=False` to preserve v0.5.x degraded-mode.
- **BREAKING**: Halt enforcement now fires across `cost.check_or_raise`,
  `gates.request`, `wrap_openai`, and `wrap_anthropic`. Add
  `except AgentHaltedError` where you catch enforcement errors for
  graceful degradation.
- **BREAKING**: Streaming token accounting reconciles at end-of-stream.
  Cost numbers will be more accurate (and often higher) than projected-only.
- See `CHANGELOG.md` for full BREAKING blocks + `Upgrade from v0.5.x` +
  `Upgrade from v0.6.0` migration playbooks.

## What's new in v0.6.0

- **Article 12 evidence export.** `governance report --format article12`
  produces an HMAC-signed Article 12 evidence bundle (`bundle_hash` +
  `bundle_signature`) from the audit trail — a single JSON bundle a
  compliance officer can hand to an auditor.
- **Ed25519 agent identity.** Per-row Ed25519 signatures over the HMAC
  audit chain, with three keystore backends (`file://`, `env://`,
  `ephemeral`) and graceful degradation to `signature_status='unsigned_local_failure'`
  when a signer cannot load its private key. The host call path never
  raises.
- **HMAC chain key rotation.** Rotate the `GOVERNANCE_AUDIT_SECRET`
  without breaking historical verification. Dual-signed rotation marker
  rows, salted fingerprint construction, bounded LRU resolution cache,
  and a `governance rotate-chain-key` CLI command.
- **Compliance pill + Article 12 evidence report.** `governance report
  --format article12` generates the EU AI Act Article 12 evidence
  record from the audit trail. The report includes a `coverage_pct`
  (with disambiguated null reason) and the new `rotation_aware`
  verification flag.
- **`kill` → `halt` rename** across the SDK and audit. Backward-
  compat aliases preserved in v0.6, removed in v0.7. See the table
  below.
- **F9 wrapper coverage registry.** Opt-in registry that records every
  wrapped LLM client at import time. Hostname stored as a salted HMAC
  digest only.

See `CHANGELOG.md` for the full release notes and `docs/migrations.md`
for the upgrade runbook (run `alembic upgrade head` before starting
v0.6 against any DB that has v0.5.x audit data).

### `kill` → `halt` rename — symbol map

All legacy names are still importable in v0.6 via identity aliases.
New code should use the halt-named symbols. **All legacy aliases will
be removed in v0.7.**

| Legacy (v0.5.x, deprecated in v0.6, removed in v0.7) | Current (v0.6+) |
|---|---|
| `AgentKilledError` | `AgentHaltedError` |
| `is_killed()` | `is_halted()` |
| `assert_alive()` | `assert_not_halted()` |
| `KillRequest` | `HaltRequest` |
| `kind='agent.killed'` audit events | `kind='agent.halted'` audit events |
| `_killed_by` / `_killed_at` metadata | `_halted_by` / `_halted_at` metadata |
| `force_refresh_killed_cache()` | `force_refresh_halted_cache()` |
| `_KILL_CACHE_TTL_SECONDS` | `_HALT_CACHE_TTL_SECONDS` |
| `_killed_cache*` | `_halted_cache*` |

Historic `agent.killed` rows stay in the HMAC chain as-is — the
append-only invariant blocks rewriting historical audit data. A SQL
view `governance_audit_events_halted` unions both kinds for downstream
queries; see the CHANGELOG monitoring-query callout.

## Framework adapters

The wrapper imports below are the canonical, supported entry points.
If you call `openai.OpenAI()` or `anthropic.Anthropic()` directly
without going through these wrappers, the call is invisible to every
SDK gate (budget, scope, audit). See the Threat Model section.

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

## CLI

```bash
governance migrate     # Apply DDL to Postgres
governance verify      # Walk HMAC chain, exit 0 (clean) or 1 (tampered)
governance tail        # Live-follow audit events
governance budget      # Show cost snapshot for an agent
governance report      # Generate EU AI Act Article 12 evidence report for actions the SDK observed
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
- **Tool invocations inside LLM responses:** Scope enforcement gates the LLM API call itself (using a sentinel action name). It does not inspect tool calls returned inside the LLM's response. An agent that receives a tool call instruction from the LLM can execute it regardless of scope policy — scope enforcement must be applied at the tool execution layer separately.

### Deployment guidance

- Route ALL LLM client instances through the SDK wrappers (`wrap_openai()`, `wrap_anthropic()`). The SDK's startup warning will flag if no wrappers are registered.
- For network-level enforcement that blocks all outbound LLM calls regardless of SDK usage, use an API gateway or proxy in front of your LLM providers.
- For Article 12 compliance evidence, the SDK logs all actions it observes. A deployment where some LLM calls bypass the wrapper will produce an incomplete evidence record.

## Configuration Reference

All options are passed as keyword arguments to `GovernanceSDK(...)` and stored on `sdk.config`.

### Module toggles

| Flag | Default | What it controls |
|------|---------|-----------------|
| `enable_audit` | `True` | **Do not disable in production.** When `False`, the HMAC chain is not persisted — the tamper-evident audit record disappears on process restart and the EU Article 12 log is silently empty. |
| `enable_scope` | `True` | Scope enforcement. When `False`, `sdk.scope` is not constructed — any call raises `AttributeError`. |
| `enable_cost` | `True` | Budget enforcement. When `False`, `sdk.cost` is not constructed. |
| `enable_gates` | `True` | HITL approval gates. When `False`, `sdk.gates` is not constructed. |
| `enable_loop` | `True` | Loop detection. When `False`, `sdk.loop` is not constructed. |
| `enable_presence` | `True` | Agent heartbeat tracking. When `False`, `sdk.presence` is not constructed. |
| `enable_prompts` | `True` | Reserved for Prompt Versioning (not yet fully implemented). Forward-compatibility flag — set to `False` only if the stub module causes issues. |
| `enable_routing` | `False` | Advisory model routing — substitutes a different model based on registered policies. Off by default to prevent silent model substitution. Requires `enable_cost=True`. |

### Audit options

| Flag | Default | Description |
|------|---------|-------------|
| `verify_chain_on_read` | `False` | When `False` (the default), tampered audit events are returned by `sdk.audit.get_events()` without raising an error — tampering is not detected until you run `sdk.audit.verify_chain()` explicitly. Set to `True` to verify the full HMAC chain on every read; raises `ChainIntegrityError` at the first broken link. Off by default because verification is O(n) in returned events — enable for compliance reporting or post-incident review. |

### Wrapper options

| Flag | Default | Description |
|------|---------|-------------|
| `warn_on_no_wrappers` | `True` | Emit a structlog warning at `sdk.start()` when no LLM wrappers (`wrap_openai`, `wrap_anthropic`) have been registered. This warning is the only startup signal that enforcement is not covering your LLM calls — silencing it in a production deployment that expects wrappers will hide a misconfiguration. Set to `False` only in intentionally wrapper-free deployments (audit-only, gate-only) or test suites. |
| `default_max_tokens` | `None` | Default `max_tokens` used by budget projection when the caller does not declare it on the API call. Suppresses the `max_tokens_not_declared` warning for projects that always use the same cap. Must be `>= 1`. |

```python
# Audit-only deployment — no wrapper, no warning
GovernanceSDK(
    database_url="postgresql://...",
    warn_on_no_wrappers=False,
)

# Disable loop detection and presence for a lightweight deployment
GovernanceSDK(
    database_url="postgresql://...",
    enable_loop=False,
    enable_presence=False,
)

# Enable forward budget projection with a default cap
GovernanceSDK(
    database_url="postgresql://...",
    default_max_tokens=4096,
)

# Verify HMAC chain on every read (for compliance reporting)
GovernanceSDK(
    database_url="postgresql://...",
    verify_chain_on_read=True,
)
```

## Running the live test suite

`scripts/live_test.py` exercises every SDK feature against a real Postgres
instance and a real OpenAI endpoint. It is the authoritative pre-release
check: unit tests alone will not catch packaging, pool-lifecycle, or
chain-integrity regressions that only surface end-to-end.

Two environment variables are **required**. The script has no fallbacks:
it fails loud if either is missing. This is a deliberate security
property — hardcoded credentials have leaked into CI logs historically,
so the guard in `scripts/test_no_hardcoded_creds.py` scans the tree and
fails CI on any literal secret.

```bash
# Required: a throwaway Postgres the test owns end-to-end.
# Spin one up locally if you don't have one:
#     docker run --rm -d -p 5435:5432 \
#       -e POSTGRES_USER=livetest \
#       -e POSTGRES_PASSWORD="$(python -c 'import secrets; print(secrets.token_hex(16))')" \
#       -e POSTGRES_DB=governance postgres:16
export GOVERNANCE_TEST_DATABASE_URL=postgresql+asyncpg://<user>:<pass>@localhost:5435/governance

# Required: a stable 32-byte HMAC key. The ephemeral-per-run path was
# removed because chain-integrity bugs that only reproduce across runs
# with the same key are invisible if the key rotates every run.
export GOVERNANCE_TEST_AUDIT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"

# Required: OpenAI credentials. Test 4 makes a real API call.
export OPENAI_API_KEY=sk-...

python scripts/live_test.py
```

The run exercises audit (HMAC chain verification), scope, cost (with
auto-pricing), budget gates, a real wrapped OpenAI call, loop detection,
agent presence, behavioral contracts, built-in model pricing, per-model
cost breakdowns, hot-reload config, and an Article 12 compliance report.
Exit code is `0` on full pass, `1` on any failure.

If you see `FATAL GOVERNANCE_TEST_DATABASE_URL is not set` or
`FATAL GOVERNANCE_TEST_AUDIT_SECRET is not set`, export the missing env
var and retry — the script never falls back to a default.

## Documentation

Full documentation, quickstart guide, API reference, and concepts:

**[www.codeatelier.tech](https://www.codeatelier.tech/governance?utm_source=github&utm_medium=readme&utm_campaign=sdk_repo)**

## License

MIT
