# Changelog

## v0.5.1 (2026-04-12)

Hotfix release covering four findings from a product-wide DX audit: three systemic
opt-in / activation-consistency bugs and one silent HITL failure in Postgres deployments.
No audit trail data was lost or corrupted — the HMAC chain is intact regardless of
these issues.

### Security

- **HITL gates were silently broken on Postgres backends.** `ContractsModule._check_hitl_approved`
  returned `False` unconditionally for `PostgresGatesStore`, causing **HITL-gated actions to
  be blocked even after human approval** (over-blocking, not bypass). Any contract with a
  `PreCondition(check="hitl_approved", ...)` would have emitted a flood of
  `contract.pre_violation` audit events for legitimately approved actions. Fix: new
  `GatesStore.has_granted_approval(agent_id)` abstract method, implemented with a SQL
  query in Postgres and strict agent_id + expiry filtering in the in-memory store.
- **`ScopeModule.filter_tools` silently returned the full tool list when no policy was
  registered**, bypassing `hidden_tools` for unregistered agents and contradicting the
  module's documented default-deny contract. Fix: raises `PolicyNotRegistered` instead.
  The LangChain handler catches the new exception and fails closed (drops the tool list
  entirely rather than passing it to the LLM).

### Breaking changes

- **`enable_audit`, `enable_scope`, `enable_cost`, `enable_gates`, `enable_prompts` flags
  are now honored.** In v0.2–v0.5.0 these flags were accepted by `GovernanceConfig` but
  never read — modules were constructed unconditionally regardless of flag value. Now
  `sdk.scope` / `sdk.cost` / `sdk.gates` / `sdk.contracts` do not exist when their flag
  is `False`; calling them raises `AttributeError`. `enable_audit=False` swaps the audit
  substrate to an in-memory ring buffer with no persistence (the attribute stays because
  every other module needs it to log events). Contracts cascades off when scope or cost
  is off; routing cascades off when cost is off. **Customers who set any of these flags
  expecting them to disable the corresponding module should review their deployment
  immediately.**
- **`ScopeModule.filter_tools("unknown_agent", ...)` now raises `PolicyNotRegistered`**
  instead of returning the full tool list. Callers that previously relied on the
  pass-through behaviour must register a policy for every agent or catch the exception
  explicitly.

### New modules

- **Routing (Model Selection Policy)** — advisory `sdk.routing.suggest()` that can
  remap the requested model based on remaining budget (`cost_aware`) or an explicit
  rewrite table (`rules`). Off by default: both `enable_routing=True` at SDK init AND
  at least one registered `RoutingPolicy` are required for routing to touch the LLM
  call path. Honors `ScopePolicy.allowed_models` as a hard constraint. Emits
  `routing.policy_changed` (on register) and `routing.suggestion` (on every model
  substitution) audit events in the HMAC chain. Wraps `wrap_openai` and `wrap_anthropic`
  transparently — no caller code changes.

### Fixes

- **`asyncio.run()` no longer called from the sync registration path** in scope, cost,
  and routing modules. Policies registered before the event loop is running are now
  queued in `_pending_upsert_policies` and drained by `flush_pending_upserts()` during
  `sdk.start()`. Removes a hidden sync-over-async that could deadlock sync startup in
  codebases owning an outer loop (violated architectural invariant #3).
- Background policy-upsert tasks now hold strong references via per-module
  `_pending_upsert_tasks` sets so Python's GC cannot collect them mid-execution.
- `ScopePolicy` gains an `allowed_models: frozenset[str]` field — a hard ceiling that
  routing cannot exceed regardless of budget state.

### Tests

- 322 → 356 tests (+21 routing module, +13 hotfix regression pins in
  `tests/test_hotfix_v0_5_1.py`).
- Full suite: 356 passed, 0 failures, 0 regressions.

## v0.5.0 (2026-04-12)

### Security

- **Self-approval prevention (fail-closed)** — HITL gates now compare the
  granting `operator_id` against the session's `user_id`; an agent cannot
  approve its own action. Requests with no `operator_id` return HTTP 403
  with an actionable error. DDL adds `operator_id` column to the gates
  table.
- **Chain fork detection** — `audit.trace_session_chain` raises
  `ChainIntegrityError` when two events share the same `prev_hash`,
  surfacing tamper attempts or concurrent-write corruption that would
  otherwise go unnoticed.
- **Additional security-critical coverage** — tests for budget race at
  the cap boundary, SQL injection payloads on every user-controllable
  field, case-sensitivity scope bypass, production error leakage
  through `sanitize_db_error`, weak-secret entropy rejection, account
  enumeration parity.

### New

- **`GovernanceSDKSync`** — sync facade for Flask/Django and any
  non-async host application. Runs an asyncio event loop on a
  background thread and dispatches via `run_coroutine_threadsafe`.
  Matches the async SDK surface one-to-one.
- **SSE endpoint** — `GET /api/stream/events` delivers live audit
  events to the console via Server-Sent Events (polling-based, session
  auth, keepalive frames).
- **Halt agent UI** — renamed from "kill" because the SDK blocks gates,
  it does not terminate the host process.
- **Multi-agent OpenAI integration test script** exercising
  delegation-style workloads end to end.

### Performance

- **Shared engine pool** — consolidated seven separate `AsyncEngine`
  instances into one shared pool per SDK instance. Dropped
  Postgres max connections per SDK from ~74 to ~15.
- **Concurrent audit writes** — pre-call audit log is backgrounded
  and post-call audit + cost tracking run under `asyncio.gather`,
  saving 4–12 ms per LLM call on the critical path.
- **Combined budget query** — session + daily counter reads merged
  into a single round-trip in `PostgresCostStore`, halving pre-call
  enforcement latency.

### Fixes

- Streaming cost-tracking bypass now detected and logged (users must
  call `sdk.cost.track()` manually after consuming the stream).
- Serverless cold-start policy preload in `sdk.start()` eliminates
  the 30-second gap where `_policies` was empty on first request
  (critical for AWS Lambda).
- JSONL audit fallback tolerates read-only filesystems and rotates
  at 50 MB.
- Session time budget uses Postgres-side elapsed computation to avoid
  mixed-clock skew between app and DB servers.
- Sync wrapper coroutine-leak fix in the Anthropic/OpenAI integrations.
- Policy upsert SQL cast corrected (`::jsonb` → `CAST AS jsonb`) so
  scope and budget policies persist across restarts.
- Top-level `__init__.py` exports `ScopePolicy`, `BudgetPolicy`,
  `AuditEvent` — no more deep-import friction for callers.
- `command_timeout=5` on the shared engine prevents pool exhaustion
  under slow-query storms.

### Tests

- 258 → 322 tests (+64). New suites: streaming detection, JSONL
  fallback, cold start, sync wrapper, console endpoints, SSE, error
  handling, normalize_db_url, sanitize_db_error, SQL injection,
  case-sensitivity bypass, chain fork detection, end-to-end
  enforcement. Test suite runs in ~5 s (was ~12 s).

## v0.4.0 (2026-04-10)

### New modules
- **Behavioral Contracts** — pre/post conditions on tool calls with built-in checks (hitl_approved, budget_available, scope_allowed, audit_logged) and custom check registry
- **Compliance Reports** — `governance report --format article12` auto-generates EU AI Act Article 12 artifacts from audit trail

### New integrations
- **Anthropic SDK adapter** — `wrap_anthropic()` with auto-audit, auto-cost via pricing table, session_id sharing
- **PyPI publish workflow** — `pip install codeatelier-governance` ready

### Quality sprint (8 fixes)
- Fixed session_id bug in OpenAI/Anthropic/LangChain wrappers (was creating fresh UUID per call, breaking per-session budget limits)
- Fixed cost status WARN logic (no longer warns on any spend > $0)
- Added auto-refresh (10s) on all console pages
- Fixed CLI password warning ("12 chars" → "8 chars")
- Fixed LoginPage CLI command hint
- Added model field on AuditEvent in wrappers (first-class HMAC chain field)
- Added model breakdown view on cost page
- LangChain handler now estimates USD via pricing table

## v0.3.0 (2026-04-10)

### New modules
- **Loop Detection** — sliding window on repeated tool calls per session, case-insensitive, configurable action (raise/log)
- **Agent Presence** — live/idle/unresponsive heartbeat tracking with stale detection
- **Policy Hot-Reload** — asyncio task polling governance_policies every 30s, atomic dict replacement

### Enhancements
- Per-model cost aggregation (`sdk.cost.model_breakdown()`)
- Console endpoints: `GET /api/cost/models`, `GET /api/agents/presence`

## v0.2.3 (2026-04-10)

### Security
- Login rate limiting (5 attempts/IP/60s, HTTP 429 with Retry-After)
- Gate audit events routed through HMAC chain (was raw INSERT)
- Metadata size cap (64KB) on `emit_audit.py`
- Request model size constraints (username 256, password 1024)

## v0.2.2 (2026-04-10)

### New features
- **G15: Session time limits** (`per_session_seconds` on BudgetPolicy)
- **G3: Built-in model pricing** (24 models, prefix matching, `track_usage()`)
- **G10: Hidden tool policies** (`hidden_tools` on ScopePolicy, `filter_tools()`)
- Console auth model (PBKDF2-HMAC-SHA256, Postgres sessions, RBAC)
- LangChain enforce mode (`enforce=True`)
- OpenAI wrapper auto-USD via pricing table, double-wrap sentinel
- CLI `emit_audit.py` for agent pipelines
- CLI user management (`governance console add-user/list-users/disable-user/reset-password`)

### Console
- Full brand overhaul (Code Atelier violet, Inter/JetBrains Mono)
- Login page, user management, NavBar, loading/empty/error states

## v0.2.1 (2026-04-09)

### Fixes
- Recursive metadata redaction
- LangChain enforce mode prep
- Pagination on event queries
- Security hardening

## v0.2.0 (2026-04-08)

### Initial public features
- Audit + Provenance (HMAC-SHA256 chain)
- Scope Enforcement (whitelist, default-deny)
- Cost Tracking (budget gates, fail-closed)
- HITL Gates (signed tokens, grant/deny)
- Console GUI (FastAPI + Next.js)
- Framework adapters (LangChain, OpenAI)
- CLI (migrate, verify, tail, budget)
