# Changelog

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
