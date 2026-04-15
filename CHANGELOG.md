# Changelog

## v0.6.0 (2026-04-15) — Ed25519, HMAC rotation, wrapper coverage, backend wiring

Major release implementing F2–F9 of the v0.6 PRD across the SDK and console.

### ⚠️ Required upgrade step

**Run `alembic upgrade head` before starting the v0.6 SDK in any environment
that has v0.5.x audit data.** The v0.6 `PostgresAuditStore` writes to the new
`signature`, `signing_key_fingerprint`, and `signature_status` columns on
`governance_audit_events`. Against a pre-migration v0.5.x schema those columns
do not exist and `AuditModule.log()` degrades to `StoreUnavailableError` —
audit rows are silently dropped until the migration is applied.

The `[migrations]` extra is required to run alembic against Postgres because
the SDK's runtime driver is `asyncpg` (async-only), and alembic's sync env.py
needs a sync driver:

```
pip install code-atelier-governance[migrations]
alembic upgrade head    # singular — a merge migration unifies the v0.6 heads
```

A pre-existing append-only grants gap on `governance_audit_events` is also
closed in this release (CLAUDE.md invariant 2). After upgrading, the
`/health/governance` endpoint should report `append_only_grants_ok: true`.

### Added

- **F2.5 full `kill` → `halt` rename** across SDK, console, and audit.
  Backward-compat aliases (`KillRequest`, `AgentKilledError`, `is_killed`,
  `assert_alive`, `force_refresh_killed_cache`, `_killed_cache`,
  `_KILL_CACHE_TTL_SECONDS`, and the `POST /api/agents/{agent_id}/kill`
  route) are preserved for v0.6 and **REMOVED in v0.7**. Historic
  `agent.killed` audit rows stay in the HMAC chain as-is (append-only
  invariant #2); a SQL view `governance_audit_events_halted` unions
  both `agent.killed` and `agent.halted` kinds for downstream queries.
- **F6 Track A — Ed25519 agent identity** with three keystore backends
  (`file://`, `env://`, `ephemeral`), per-row signatures over the HMAC chain,
  append-only key registry and revocation tables, and graceful degradation
  to `signature_status='unsigned_local_failure'` if the signer cannot load
  its private key (host call path never raises).
- **F6 Track B — HMAC chain key rotation** with dual-signed rotation marker
  rows (outgoing + incoming MAC), salted fingerprint construction
  (`HMAC-SHA256(key, 'codeatelier.fingerprint.v1')`), bounded LRU resolution
  cache (max 64 entries), and a `rotate-chain-key` CLI command. Missing key
  material resolves to `chain_integrity_status='unverified'`, surfaced via
  `/health/governance`.
- **F6#4 per-user rate limiting** on `/api/policies`, `/api/policies/{id}`,
  `/api/events/stats`, `/api/agents/presence`, `/api/gates/pending`, and
  `/api/gates/recent`. Default 60 req/min/user, configurable via
  `GOVERNANCE_CONSOLE_USER_RATE_LIMIT`.
- **F9 wrapper coverage registry** (opt-in via `enable_coverage=True` in
  `GovernanceConfig`). New `governance_wrapper_registrations` table
  (mutable state, NOT audit), in-memory primary + Postgres mirror, hostname
  PII removal via salted hashing, instance UUID PK to defeat PID reuse,
  opportunistic 30-day prune, and a new `GET /api/coverage` endpoint.
- **F3 backend endpoint wiring** — all 9 prior orphan endpoints now use
  Pydantic response models with `extra="forbid"` and pass through a
  recursive secret-redaction layer (`sk-ant-`, `sk-`, `xoxb-`, `gh[ps]_`,
  AWS keys). Session DELETE now emits a `pipeline.session_revoked`
  audit event.
- **F2 P0 console fixes** — halt 404 fixed, SSE ghost-field hydration via
  lazy `GET /api/events/{event_id}` with typed `AuditEventView`, truncation
  bucket relabel, DisconnectBanner consolidation with `rankWorst()`,
  `KillRequest.reason` validator (NFC normalize, 512-char cap, escape
  `\\` first then `\n\r\t`, strip C0 controls).
- **F6#5 sanitizer hardening** — `sanitizeErrorMessage` now strips DSNs,
  Unix and Windows filesystem paths, IPv4 and IPv6 addresses in addition
  to the existing auth-keyword patterns.
- **F7 pipeline hygiene** — new `GET /health/governance` (anon: status only;
  authed: chain-integrity + key resolution state + p50/p95 latency),
  `automation/lib/validate_cron_artifacts.py` (CI not cron), pre-commit
  TODO gate at `.githooks/pre-commit-todo-gate.sh` (allows version-tagged
  `TODO(vN.M.P):`), `Makefile install-hooks` target, `CODEOWNERS` at repo
  root, `V3DeprecationBanner` mounted in the v3 root layout, and CI jobs
  for `tsc --noEmit`, `validate-cron-artifacts`, `console-version-parity`.
- **Migrations runbook** at `docs/migrations.md`.
- **Integration test** at `tests/integration/test_migration_against_seeded_db.py`
  that spins up a real Postgres in Docker, seeds v0.5.x-shaped audit rows,
  runs `alembic upgrade head`, and asserts the migration backfill, the
  append-only enforcement, and the post-migration default behavior. Marked
  `@pytest.mark.integration` so it is skipped by default.

### Changed

- Console version bumped from `0.4.0` to `0.5.0` (`app.py` + `package.json`).
- `compliance/models.py::ComplianceReport` now carries
  `coverage_pct_reason: Literal["no_scope_policies_registered","registry_disabled","ok"] | None`
  to disambiguate `coverage_pct=None` between "registry disabled" and
  "denominator is zero."
- `migrations/env.py` now coerces `postgresql://` and `postgresql+asyncpg://`
  URLs to `postgresql+psycopg://` at runtime so alembic can run with the
  optional `[migrations]` extra installed.

### Security

- New tables `governance_agent_keys`, `governance_agent_key_revocations`,
  `governance_audit_chain_keys` are append-only at the grant level
  (`REVOKE UPDATE, DELETE FROM PUBLIC`).
- New migration `978884c6b7f1_revoke_audit_grants.py` closes a pre-existing
  gap on `governance_audit_events`: the append-only trigger has been in
  place since v0.1, but the role-level grants were never revoked. v0.6
  brings the original audit table to parity with the new identity tables.
- `cryptography>=42.0,<46.0` added as a direct runtime dependency
  (Cybersecurity-approved, see `.agent-outputs/cybersecurity/`).

### Known limitations

- **F4 `unresolved_fingerprints` is NOT a verification signal in v0.6.**
  The field is always `[]` on the single-key verifier path and is only
  populated when `rotation_aware=true` (F6 Track B rotation-aware
  verifier), which is not wired into the console handlers in this
  release. An empty list MUST NOT be interpreted as "all keys verified
  successfully". The new `rotation_aware` boolean on
  `ComplianceReportView` / `VerifyChainResponse` makes the distinction
  explicit; wiring the rotation-aware path ships in v0.6.1.
- 6 v0.6 PRD features remain pending and ship in v0.6.1: F1 console
  honesty pass, F2.5 full `kill`→`halt` rename, F4 compliance console
  surface, F5 HITL approval queue panel, F8 code quality remainder
  (TS literal narrowing, hand-rolled SSE validator, 4 critical v4 tests),
  and F6 bundle items 3 (audit write rate limiting) and 6 (tenant-scoped
  query keys).
- The v3 console remains the default. The v4 IA shell exists under
  `console/src/app/(v4)/` and is opt-in via
  `NEXT_PUBLIC_CONSOLE_UI_VERSION=v4`. The default flips to v4 once F1
  wires the F3 backend consumers and un-stubs `useAgentPolicy`.
- `JsonlFallbackStore` does not yet round-trip the new signature columns;
  rows recovered from the disk fallback degrade to `signature_status='unsigned'`
  (constraint #7: not a chain break).

## v0.5.4 (2026-04-14) — kill switch enforcement hotfix

Emergency P0 hotfix. Closes a shipped production bug in v0.5.3 where the console
"Halt" button (backend route `/api/agents/{agent_id}/kill`) wrote a kill marker
into `governance_agent_presence.metadata_json` and emitted an `agent.killed`
audit event, but the SDK enforcement path **never read the kill marker**. Result:
operators clicking Halt saw an audit log entry and a status badge change while
the agent kept running. The CLAUDE.md kill-switch requirement was violated for
every v0.5.3 deployment.

### Security

- **Kill switch now actually halts the agent.** `sdk.scope.check()` calls
  `presence.assert_alive(agent_id)` first, raising `AgentKilledError` if the
  agent has been killed by an operator via the console. The check reads from a
  5-second TTL in-memory cache backed by `governance_agent_presence`, so the hot
  path stays fast (one dict lookup + one timestamp comparison) and the worst-case
  delay between kill click and enforcement is bounded by the cache TTL.
- **Detection uses metadata, not status.** Kill state derives from
  `metadata_json->>'_killed_by' IS NOT NULL`, NOT from `status='unresponsive'`.
  The status column is overloaded by `check_stale()` (heartbeat timeouts), so
  gating on it would also block agents that simply went idle. The metadata
  marker is the unambiguous kill signal.
- **Invariant #1 preserved.** When the governance DB is unreachable, the kill
  cache holds the last known state and a warning is logged — already-killed
  agents stay killed, live agents stay live, and the host application keeps
  running. The refresh path never raises out to the caller.

### What's new

- `codeatelier_governance.presence.AgentKilledError` — raised by the SDK when
  an action is attempted by a killed agent. Distinct exception type so callers
  can differentiate from `ScopeViolation`, `PolicyNotRegistered`, etc.
- `PresenceModule.is_killed(agent_id) -> bool`
- `PresenceModule.assert_alive(agent_id) -> None`
- `PresenceModule.force_refresh_killed_cache()` — bypass TTL (used by tests
  and by future LISTEN/NOTIFY-driven invalidation in v0.6)
- `ScopeModule.set_presence_module(presence)` — wired automatically by
  `GovernanceSDK.__init__` when `enable_presence=True` AND `enable_scope=True`

### Tests

`tests/presence/test_kill_switch.py` — 28 tests covering happy path, cache
TTL behaviour (including 50-concurrent-call double-checked-locking under load),
Invariant #1 DB-outage resilience, in-memory mode, edge cases (unicode IDs,
500-agent scaling, partial metadata, un-kill via metadata removal, idempotent
re-kill), and ScopeModule integration with ordering checks (kill check fires
BEFORE PolicyNotRegistered and BEFORE the tool/api ValueError).

### What is NOT in this hotfix (intentional)

- **Only `scope.check()` is patched.** `cost.check_budget()` and `gates.check()`
  should also call `assert_alive()`. v0.5.4 ships scope only because it's the
  most-called gate; cost+gates land in v0.6.
- **No LLM-wrapper enforcement.** `wrap_anthropic()` / `wrap_openai()` should
  call `assert_alive()` before send. Same v0.6 deferral.
- **No "un-kill" admin endpoint.** Restore by clearing `_killed_by`/`_killed_at`/
  `_kill_reason` from `governance_agent_presence.metadata_json`. v0.6 console
  gets a "Restore" button.
- **Cache invalidation is TTL-only.** No Postgres LISTEN/NOTIFY. 5-second
  worst-case delay between kill click and enforcement. v0.6 adds push-based
  invalidation if needed.
- **Naming kept as `kill` everywhere.** Per memory `project_v05_prd.md` the
  product name was approved as "halt" in the v0.5 PRD, but renaming the wire,
  audit-event-kind, metadata fields, exception class, and SDK methods in a
  hotfix would be a chain-history breaking change. The full `kill` → `halt`
  rename is documented in the v0.6 PRD as F2.5 with a migration script for
  the `agent.killed` → `agent.halted` audit event kind. v0.5.4 keeps the old
  names for hotfix safety; v0.6 ships the rename with backward-compat aliases.

### Migration

None required. Drop-in upgrade from v0.5.3:

```bash
pip install --upgrade code-atelier-governance==0.5.4
```

The cache TTL constant `_KILL_CACHE_TTL_SECONDS = 5.0` is module-level and
not yet runtime-configurable; v0.6 will expose it via `GovernanceSDK.config`.

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
