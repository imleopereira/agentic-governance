# Changelog

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
