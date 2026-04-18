# Changelog

## v0.6.2 (unreleased) — v4 console default flip + 5 P0 enforcement fixes

Patch release. Lands the four parked Wave 1.5 worktrees from the v0.6.1
polish sprint, flips the default console UI from v3 to v4, and bundles
five P0 fixes uncovered during the v0.6.1 post-ship team review and a
subsequent live demo walkthrough:

1. Wheel-packaged migrations (fresh installs were silently skipping alembic).
2. Grant/deny TOCTOU hardening (mirror of v0.6.1 escalate fix).
3. Halt enforcement expanded across cost + gates + LLM wrappers.
4. Grant/deny token HMAC verification (broken since v0.2, caught live).
5. Session-drawer chain verify is now rotation-aware (pre-rotation events
   no longer render as `verified=false` after `rotate-chain-key`).

Phase 2 (creative-audit follow-up) additionally lands: rotation-marker
dual-MAC in session verify, bounded/paginated `/api/gates/pending` (+
new `/api/v2` paged route), chunked verify off the event loop, serialized
gate+audit, end-of-stream reconciliation for Anthropic + OpenAI wrappers
(closes abandonment, cancellation, and double-reconcile vectors),
`claude-opus-4-7` pricing, strict-by-default `CostModule`/`RevocationStore`/
`InMemoryAuditStore`, and v2 gate tokens (opt-in in v0.6.2).

No new migrations. **Several public SDK defaults flip this release.**
See the four BREAKING blocks below before upgrading.

> **BREAKING DEFAULTS** — four defaults flipped to fail-closed instead
> of silent-degrade. If your code caught exceptions broadly or relied
> on silent-zero accounting, review these before upgrading.
>
> - `CostModule.strict_unknown_models=True` — unknown model names raise
>   `UnknownModelError` instead of returning `0.0`. Add pricing entries
>   (`cost/pricing.py::MODEL_PRICING`) or set
>   `cost_strict_unknown_models=False` with a fallback rate. LangChain
>   handler is already wired to observe-only mode (never raises);
>   direct SDK callers see the raise. New env vars:
>   `GOVERNANCE_COST_STRICT_UNKNOWN_MODELS`,
>   `GOVERNANCE_COST_UNKNOWN_MODEL_FALLBACK_USD_PER_MILLION`.
> - `InMemoryAuditStore.on_full="raise"` — hitting `max_events` raises
>   `StoreUnavailableError` instead of silently evicting. Callers
>   needing ring-buffer semantics must pass `on_full="evict"`.
>   `BatchingWriter`'s internal fallback opts into eviction automatically.
>   `.stats()` + `.verify_not_truncated()` are new.
> - `RevocationStore.strict_chain=True` — when the audit write inside
>   `revoke_with_chain_event` fails, the revocation now RAISES
>   (previously it wrote the row with `chain_event_id=None`). Pass
>   `strict_chain=False` to preserve degraded-mode behavior; WARN is
>   logged as `identity.revocation_without_chain_event`.
> - Gate resolve + audit event now run through a single serialized
>   path (`on_commit` hook). The narrow residual window (audit
>   commits, gates rolls back) is documented and detectable via
>   duplicate `approval.granted` chain rows on one `request_id`.
>   True cross-engine atomicity is a v0.7+ target.

> **BREAKING WIRE CONTRACT** — `/api/gates/pending` + verify failure
> reasons.
>
> - `/api/gates/pending` (v1) is now admin-only and caps at 500 rows.
>   Response shape is unchanged (plain array) but carries
>   `Deprecation: true`, `Sunset: Thu, 01 Oct 2026`, and
>   `X-Truncated: true` when the cap is hit. Migrate to
>   `/api/v2/gates/pending` which returns `{items, has_more, next_cursor}`
>   with keyset pagination (cursor format: `"<iso_ts>|<request_id>"`).
> - Per-agent reviewer scope is deferred to v0.6.3; `admin` is the
>   only role with access today. Grant/deny were already admin-only,
>   so read-scope now matches act-scope.
> - `GET /api/session/{id}/verify` may return new `failure_reason`
>   values (`rotation_marker_dual_mac_failed`,
>   `rotation_marker_no_incoming_key`,
>   `rotation_marker_unresolved_incoming:<fp16>`,
>   `rotation_marker_without_key_versions`,
>   `unresolved_key:<fp16>`, `hmac_mismatch`). New top-level
>   `first_failure_reason` field scopes the first breaking event.

> **BREAKING BEHAVIOUR (gate token format)** — gate approval tokens
> gain a v2 format (opt-in in v0.6.2, default in v0.6.3).
>
> - `GatesModule(enable_v2_tokens=False)` is the default in v0.6.2.
>   Tokens mint in legacy v1 format. Set `enable_v2_tokens=True`
>   (or env `GOVERNANCE_GATES_ENABLE_V2_TOKENS=true`) to opt in to
>   the `v2:<key_prefix>:…` format, which binds tokens to the HMAC
>   key version active at issue time — pre-rotation tokens keep
>   verifying after `rotate-chain-key`. Both formats verify in v0.6.2.
> - v1 tokens accepted until `accept_v1_until` (default: ~2026-07-17,
>   90 days post-release). After that cutoff, v1 tokens raise
>   `TokenVersionTooOldError`. Override via
>   `GatesModule(accept_v1_until=...)` or env
>   `GOVERNANCE_GATES_ACCEPT_V1_UNTIL`. Pre-cutoff parses emit
>   `DeprecationWarning` and `gates.legacy_token_parsed` INFO events.
> - v0.6.3 will flip `enable_v2_tokens=True` as the default. Rolling
>   deploys spanning that boundary should set the flag explicitly.

> **BREAKING BEHAVIOUR (streaming accounting)** — Anthropic + OpenAI
> wrappers.
>
> - Streaming responses reconcile actual token usage at end-of-stream.
>   Previous releases tracked only the projected `max_tokens` value.
>   Customers with hard USD caps should expect more accurate (and
>   often higher) cost numbers.
> - Abandoned streams (proxy returned but never iterated) emit
>   `governance.stream.abandoned_without_reconcile` WARN and
>   best-effort reconcile.
> - Mid-stream exceptions and `asyncio.CancelledError` reconcile with
>   a conservative `chunks × 8` token estimate instead of silent
>   projected-wins — closes an agent-initiated budget-bypass vector.
> - OpenAI `chat.completions.create` wrappers auto-inject
>   `stream_options={"include_usage": True}` unless the caller
>   explicitly sets it. Explicit `include_usage=False` is honored
>   but logs a one-shot WARN.
> - Reconciling proxy preserves `isinstance()` identity with the
>   wrapped stream class.

> **BREAKING BEHAVIOUR (halt enforcement):** Calling any enforcement
> path — `cost.check_or_raise`, `gates.request`, `wrap_openai`,
> `wrap_anthropic` — against a halted agent now raises
> `AgentHaltedError`. On v0.5.4–v0.6.1 these paths silently succeeded
> because only `scope.check` was wired into halt. If your application
> catches and ignores enforcement errors to allow graceful
> degradation, add an explicit `except AgentHaltedError` guard
> before upgrading. Tokens minted BEFORE the halt still resolve
> through `gates.grant`/`gates.deny`, since the reviewer — not the
> agent — is the principal on resolution.

> **BREAKING DEFAULT**: The console now loads v4 on first visit.
>
> **Run-time rollback (recommended):** append `?ui=v3` to any console
> URL. This persists via a `console_ui_version` cookie (SameSite=Lax,
> Secure in production, 30-day max-age) and works on pre-built Docker
> images and bundled PyPI wheel assets. This is the escape hatch if
> you cannot rebuild the console.
>
> **Build-time rollback (only if you rebuild the console from source):**
> set `NEXT_PUBLIC_CONSOLE_UI_VERSION=v3` BEFORE `next build`.
> `NEXT_PUBLIC_*` variables are inlined at build time — setting this
> as a container/runtime env var on a pre-built image is silently
> ignored.
>
> v3 is removed in v0.7.

### Upgrade from v0.5.x

1. **Run `governance migrate`.** v0.6.0 added `signature_status` + Ed25519 columns; v0.6.0/v0.6.1 wheels failed to package the alembic files. v0.6.2 fixes that, but you still need to run the migration. **Without this, the first audit write raises `StoreUnavailableError` against the missing column.**
2. **Preserve v0.5.x cost behavior** (if you use fine-tuned or non-catalog model names — they used to silently price at `$0`, now they raise):
   ```python
   GovernanceSDK(
       cost_strict_unknown_models=False,
       cost_unknown_model_fallback_usd_per_million=10.0,
   )
   # or via env:
   #   GOVERNANCE_COST_STRICT_UNKNOWN_MODELS=false
   #   GOVERNANCE_COST_UNKNOWN_MODEL_FALLBACK_USD_PER_MILLION=10.0
   ```
3. **Preserve v0.5.x in-memory ring-buffer semantics** (if anything relies on silent eviction at `max_events`):
   ```python
   store = InMemoryAuditStore(on_full="evict", max_events=100_000)
   ```
4. **Preserve v0.5.x revocation degraded-mode** (if you tolerate unlinked revocations when the audit write fails):
   ```python
   RevocationStore(strict_chain=False)
   ```
5. **Add `except AgentHaltedError` guards** around any code that catches enforcement exceptions for graceful degradation. v0.5.x only raised on `scope.check`; v0.6.2 raises on `cost.check_or_raise`, `gates.request`, `wrap_openai`, and `wrap_anthropic` too.
6. **Streaming cost numbers will go up.** End-of-stream reconciliation corrects the previous `max_tokens`-only projection. Hard-USD-capped customers may see `BudgetExceeded` on workloads that passed before — this is the bypass vector closing, not a regression.

### Upgrade from v0.6.0 / v0.6.1

Steps 2–6 from the v0.5.x path apply. Skip step 1 if you already ran `governance migrate` against a live DB (alembic is idempotent).

Additional v0.6.2-only migration step to avoid a flip-day scramble when v0.6.3 flips gate tokens to v2 default:
```python
GatesModule(enable_v2_tokens=True)
# or:
#   GOVERNANCE_GATES_ENABLE_V2_TOKENS=true
```

### Added

- **Wheel-packaged migrations (P0).** `alembic.ini` and the full
  `migrations/` tree now ship inside the `codeatelier_governance`
  package and are resolved via `importlib.resources` at
  `governance migrate` time. v0.6.0 and v0.6.1 wheels omitted these
  files — fresh `pip install` users ran the base DDL, the alembic step
  silently no-op'd, the schema stayed on v0.5, and the first audit
  write raised `StoreUnavailableError` against the missing
  `signature_status` column. Anyone who already ran `governance migrate`
  on v0.6.0/0.6.1 against a live DB is unaffected (alembic is
  idempotent); fresh installs and test fixtures are the ones the fix
  unblocks.
- **Grant/deny TOCTOU closed (P0).** `POST /api/gates/:id/grant` and
  `/deny` now pin the UPDATE to the `reviewer_id` read during the
  authz check (`IS NOT DISTINCT FROM` handles the unclaimed case) and
  use `RETURNING` to detect a racing claim. A lost race now returns
  409 instead of silently granting/denying on a claim owned by a
  different reviewer. Admins bypass the reviewer pin so
  incident-response flows still work when a gate is claimed
  mid-request. Mirrors the v0.6.1 escalate hardening to the remaining
  two resolution paths; `approval.granted` / `approval.denied` audit
  rows are emitted only on a successful UPDATE.
- **Halt enforcement expanded.** `sdk.presence.halt()` now fail-closes
  every SDK enforcement path — scope, cost, gates, and the
  `wrap_openai` / `wrap_anthropic` wrappers. v0.5.4 shipped scope-only;
  the other paths retained a gap where halted agents could keep burning
  budget, claiming gates, and making LLM calls. Closes that gap.
  `AgentHaltedError` raised on any enforcement call against a halted
  agent. Exception: operator-facing `gates.grant()` / `gates.deny()` on
  tokens minted BEFORE the halt still resolve, because the reviewer —
  not the agent — is the principal on grant/deny.
- **Grant/deny token HMAC verification (P0, silently broken since v0.2).**
  The console's `grant_gate` / `deny_gate` handlers were comparing the
  full signed token (`{uuid}:{action_hash}:{iso}:{hmac_hex}`) against a
  self-computed HMAC of just the `request_id` — two different shapes,
  always mismatched, so any real SDK-minted token rejected with
  "Token HMAC verification failed." Invisible to every unit test because
  every mock gate row used `token: None`, skipping the verify branch.
  Caught during a live demo walkthrough. Fix: dogfood the SDK's own
  `parse_token` from `gates.tokens` and cross-check `request_id` AND
  `action_hash` on both grant and deny. Security strengthened: now
  rejects signature tampering, expiration, wrong-request, AND
  action_hash tampering. +5 regression tests in
  `tests/console/test_gates_token_verification.py`.
- **Session-drawer chain verify is rotation-aware (P0).**
  `GET /api/session/{id}/verify` now loads `governance_audit_chain_keys`
  and verifies each row under the key active at ITS `chain_seq` rather
  than under the single current `AUDIT_SECRET`. Pre-fix, any customer
  who ran `cga rotate-chain-key` would see legitimate pre-rotation
  events as `verified=false` in the UI session drawer — the same bug
  that bit the v0.6.2 demo seed before rotation was dropped from it.
  Sessions that don't span a rotation see identical output. Missing
  historical key material surfaces as `verified=false` without raising.
  +3 regression tests in `tests/console/test_verify_session_rotation.py`.
- **v4 is the default console UI.** `next.config.ts`, middleware, and
  the v4 layout all fall through to `v4` when
  `NEXT_PUBLIC_CONSOLE_UI_VERSION` is unset. `/` rewrites to `/agents`
  under v4.
- **Compliance entry in the v4 sidebar.** `FileCheck` icon, routes to
  `/compliance`, lands on the Article 12 report with the Export button
  Wave 1 shipped.
- **ComplianceHeaderPill navigates to /compliance on click.** The
  pill's primary affordance is now navigation; re-verify moves to an
  adjacent keyboard-reachable icon button with an explicit
  `aria-label="Re-verify chain integrity"`. Auto-reverify on stale and
  the in-flight guard are unchanged.
- **Officer-voice empty-state copy** for the v4 drill panels
  (`empty-states.ts`): no version callouts, no raw Python, one
  actionable sentence per state targeted at compliance officers.
- **`console_ui_version` cookie persistence.** The `?ui=v3` / `?ui=v4`
  query override now survives subsequent navigations via a 30-day
  `SameSite=Lax` cookie. Precedence: query → cookie → env var →
  default `v4`. The query param is stripped from the URL on the
  persist-redirect.
- **Playwright v4-default smoke test.** `tests/e2e/v4-default.spec.ts`
  (three tests: default `/` → v4, Compliance nav + export button,
  `?ui=v3` cookie). A new `console-e2e-smoke` CI job runs it on
  console-touching PRs with `continue-on-error: true` while we collect
  flake stats. Promote to blocking in v0.7.
- **UTC timestamps across the console.** `LiveBadge`, `/cost`
  last-updated, v3 root agent-card, `/stream` event timestamps, and
  the compliance page all render UTC uniformly (DB + audit chain are
  UTC; mixing local time in the UI was rehearsal-bait). New shared
  `console/src/lib/formatDate.ts` helper (`formatUtcTimestamp`,
  `formatUtcDateForFilename`).
- **Sidebar approval-count badge always visible.** Previously the
  count rendered only when `/gates` was the active route. `Sidebar.tsx`
  now runs its own `useQuery` for `api.gatesPending` (shared queryKey,
  no double-fetch), 30-s refetch, gated on `!!user`; failure renders
  no badge (never `?`); count=0 renders no badge. `font-mono`,
  `var(--warn)` bg, 9px rounded, AA contrast.
- **Compliance page redesign.** Hero-first IA: `Article 12 evidence`
  eyebrow → H1 `Chain integrity: verified` (color-coded) → single-line
  fact row → promoted Export button → inline download confirmation.
  Dev-voice leak removed (`enable_coverage=True` Python flag →
  compliance-officer phrasing). Export filename format:
  `compliance-evidence-YYYY-MM-DD_to_YYYY-MM-DD.json`.
- **Walkthrough refreshed for v0.6.2.** 6 dev-voice steps → 5
  compliance-officer-voice steps. v3 route anchors replaced with v4
  (`/agents`, `/gates`, `/compliance`, compliance-header-pill). Halt
  described via pill red-state (the drawer halt button was removed
  for a11y pre-v0.6; API-only today).

### Changed

- **V3DeprecationBanner copy flipped.** Banner (still
  `NEXT_PUBLIC_SHOW_V3_BANNER`-gated) now shows only when
  `NEXT_PUBLIC_CONSOLE_UI_VERSION=v3` is explicitly set, warning
  forced-v3 operators that v3 is removed in v0.7. Default-install
  users never see it because they land on v4.
- **Sidebar "Topology" → "Agents"** in v4 (`/agents` is the v4
  landing). v3 nav untouched to preserve the exact shipped v0.6.1
  layout for escape-hatch users.
- **ContractsPanel copy.** Removed the stale "ships in v0.6" line;
  contracts have been registerable in-process since v0.6.0. The new
  text points at the SDK registration API and notes that a per-agent
  read endpoint is on the v0.7 roadmap.
- **`console/src/app/page.tsx`** (v3 root landing) renamed the page
  component from `TopologyPage` to `AgentsPage`, fixed the H1, and
  moved the Python onboarding snippet behind `<details>` so the v3
  empty state stops shouting at non-dev visitors.

### Opt-out

- Set `NEXT_PUBLIC_CONSOLE_UI_VERSION=v3` at build time to keep v3 as
  the default. The `?ui=v3` URL param flips the `console_ui_version`
  cookie and persists across navigations. v3 is removed in v0.7.

## v0.6.1 (unreleased) — security sweep fixes

Patch release driven by a 5-agent security sweep against v0.6.0 and a
follow-on Devil's Advocate pass. No API changes; one wire-contract change
to the compliance bundle signature scheme.

### Security

- **`AuditEvent` field sanitization (S5 P1).** Every caller-controllable
  string field on ``AuditEvent`` — ``kind``, ``agent_id``, ``model``,
  ``input_hash``, ``output_hash`` — now flows through the same NFC-normalise
  + C0/ANSI-strip sanitizer as ``metadata``. Closes an insider-with-SDK-
  creds text-injection primitive against any surface that renders audit
  rows in a terminal or HTML shell (operator ``governance audit tail``
  CLI, console event list, compliance exports).
- **OTel exporter redacts secret-shaped metadata (S5 P1).**
  ``OTelExporter._build_attributes`` now applies ``redact_secrets`` to
  ``record.metadata`` before flattening values into span attributes.
  Stack traces and vendor DSNs that accidentally land in audit metadata
  will no longer leak over the host's OTel pipe (Datadog, Honeycomb,
  ...). ``redact_secrets`` moved to the new ``security/`` package so
  both ``audit`` and ``console`` can call it without cross-package
  layering violations; ``codeatelier_governance.console.redaction``
  remains importable as a back-compat alias until v0.7.
- **Database DSN pattern added to ``redact_secrets`` (DA follow-up).**
  ``postgres://``, ``postgresql://``, ``mysql://``, ``mongodb://``,
  ``mongodb+srv://``, ``redis://``, ``rediss://`` URLs (with or without
  embedded ``user:pass@`` credentials) are now redacted. Matches the
  frontend ``sanitizeErrorMessage`` DSN pass for parity.
- **``POST /api/gates/{id}/escalate`` claimant check (S3 P1).** A gate
  claimed by reviewer A can now only be escalated by reviewer A or by
  an admin (403 otherwise). Prior behaviour let any authenticated user
  — including viewers — release any reviewer's claim by NULL-ing
  ``reviewer_id``, a continuous griefing vector against the admin
  review workflow. A ``gates.escalated`` audit row is emitted on
  success only (no audit row on a 403, so viewers cannot flood the log
  by probing claimed gates).
- **Escalate TOCTOU closed (DA follow-up).** The SELECT-for-authz /
  UPDATE split in ``escalate_gate`` could let a racing writer mutate
  ``reviewer_id`` between the two statements at default READ COMMITTED
  isolation. The UPDATE now pins the expected ``reviewer_id`` via
  ``IS NOT DISTINCT FROM`` with ``RETURNING`` and 409s on a lost race
  rather than silently succeeding on stale state.
- **Console error-message sanitizer gains high-entropy passes (S5 P2).**
  ``sanitizeErrorMessage`` in ``console/src/lib/connectionStore.ts``
  now strips bare 32+ char hex runs and 40+ char base64 runs with
  entropy lookaheads (requires at least one letter and one digit so
  Tailwind class fixtures and monocharacter filler strings pass
  through). Catches fake AUDIT_SECRETs, HMAC digests, and Ed25519
  signatures that a server could echo into an error body without a
  surrounding ``token=`` keyword.

### Changed — wire contract

- **Compliance bundle signature scheme is algorithm-pinned.** Both
  ``bundle_hash`` and ``bundle_signature.signature`` now cover
  ``bundle_signature.algorithm`` and ``bundle_signature.key_fingerprint``.
  The prior scheme (v0.6.0) excluded the whole ``bundle_signature``
  sub-object from the hash input and so permitted a version-confusion
  downgrade where a holder of an old HMAC secret could re-label a
  bundle under a future signature scheme. v0.6.0 bundles will NOT
  re-verify under the v0.6.1 verifier recipe; v0.6.0 shipped ~1 hour
  before v0.6.1 so no production bundles exist under the old scheme.

## v0.6.0 (2026-04-16) — Ed25519 + HMAC rotation + Article 12 export + self-discipline

Published to PyPI: <https://pypi.org/project/code-atelier-governance/0.6.0/>.

Major release implementing F2–F9 of the v0.6 PRD plus the polish pass landed
after the initial v0.6 tag (Track A finish, LLM-theater tripwire closure,
compliance export, fresh-install fix). See **Silent / wire-contract changes**
below for additions to audit event kinds and `signature_status` values that
downstream pipelines must account for before upgrading.

### Added

- **`POST /api/compliance/export`** — signed Article 12 evidence bundle.
  Packages the existing `/api/compliance/report` and `/api/compliance/verify-chain`
  outputs into one JSON document with a sha256 `bundle_hash` and an HMAC-SHA256
  `bundle_signature` under the active `AUDIT_SECRET`. If the internal
  `verify_chain` pass raises, the bundle still emits with
  `chain_verification_error` populated — the export is evidence, not an
  enforcement gate. Shares the 1 req/60 s/user F4 rate-limit bucket.
- **"Export Article 12 evidence" button** on the v4 Compliance page wired to
  the new endpoint. Bundle filename uses a Windows-safe stamp
  (`compliance-evidence-{start}-{end}.json` with `:` replaced by `-`). Button
  is screen-reader wired with `aria-describedby` help + error + status live
  region so the download lifecycle is announced.
- **`GOVERNANCE_COMPLIANCE_RATE_LIMIT` env var** overrides the default
  1 req/60s per-user ceiling on compliance endpoints (export, report,
  verify-chain). Default unchanged.

### Changed

- **Track A — AuditModule wiring finished.** `activation_seq` is now monotonic
  (enforced at insert, not just advisory), and revocation writes append a chain
  row rather than mutating state.
- **DrillPanel** focus-management bug fix (v4 console).
- **sseValidator** comment cleanup to match the v0.6 typed SSE path.

### Self-discipline

- **LLM-theater tripwire closed.** Source-grep assertions are replaced with
  runtime tests where feasible, and CI now enforces a numeric ceiling on the
  remaining source-grep count with a monotonic-decrease rule per release.
  The count silently breached the v0.6 threshold (8 > 5); v0.6.0 restores
  discipline by making the violation loud.

### Silent / wire-contract changes

These are additive and do not break a strict v0.5.x client at the wire
level, but a downstream pipeline parsing the audit log with an exhaustive
enum MUST add the new values before upgrading.

- **`signature_status` values expanded.** The `AuditEventRecord.signature_status`
  Literal now includes `revoked_key`, `invalid_signature`, and `unknown_key`
  in addition to the v0.6.0 set. A pipeline using a strict enum on this field
  must add these values before pulling v0.6.0 audit rows.
- **New audit event kind `audit.agent_key_revocation`.** Emitted whenever a
  key is revoked via `RevocationStore.revoke_with_chain_event()`. Downstream
  filters that allow-list known event kinds must add it.
- **New audit event kind `compliance.bundle_exported`.** Emitted on every
  successful `POST /api/compliance/export`. Alerting on "unknown event kinds"
  will see this as noise until the rule is updated.
- **Console test environment changed from Node to jsdom.** Console tests that
  previously asserted `typeof window === 'undefined'` will now behave
  differently.

### Known limitations

- **No offline verifier script or `docs/verify-evidence.md` yet.** The bundle
  format is self-describing (algorithm, key_fingerprint, canonicalization via
  the public `canonical_json` helper) but a regulator-facing "how to verify
  this bundle on your own machine" recipe has not shipped. Blocks the
  "hand this to your regulator" positioning; until then the export is a
  design-partner demo artefact. Slated for v0.6.1.
- **HMAC-only bundle signature.** Bundle is signed with HMAC-SHA256 under the
  shared `AUDIT_SECRET`. An auditor must hold the secret to verify — fine
  for self-contained evidence-handoff but blocks third-party offline
  verification. Ed25519 bundle signing with a published public key is the
  v0.6.1 target.
- **`rotation_status.known_fingerprints_in_window`** is misnamed — the value
  is the UNRESOLVED fingerprints list. Rename held to v0.6.1 because the
  bundle wire contract is already shipped.
- **429 UX**: rate-limit retries surface a generic error rather than parsing
  `Retry-After`. Product-level UX decision pending.
- **Compliance export rate limit (1 req/60s/user)** is tight for officers
  running bundle exports across multiple windows. Overridable via
  `GOVERNANCE_COMPLIANCE_RATE_LIMIT` for environments that need higher
  throughput. The bundle is embedded in the signer's audit chain regardless.

### ⚠️ Required upgrade step (v0.5.x → v0.6)

**Install the `[migrations]` extra and run `governance migrate` (or
`alembic upgrade head`) before starting the v0.6 SDK in any environment
that has v0.5.x audit data.** The v0.6 `PostgresAuditStore` writes to the
new `signature`, `signing_key_fingerprint`, and `signature_status` columns
on `governance_audit_events`. Against a pre-migration v0.5.x schema those
columns do not exist and `AuditModule.log()` degrades to
`StoreUnavailableError` — audit rows are silently dropped until the
migration is applied.

```bash
pip install "code-atelier-governance[migrations]==0.6.0"
governance migrate --database-url postgresql://...
# Equivalent: alembic upgrade head (manual path; governance migrate now
# runs alembic automatically after applying the base DDL).
```

The `[migrations]` extra is required because the SDK runtime driver is
`asyncpg` (async-only) and alembic's sync env.py needs a sync driver.
A merge migration unifies the three parallel v0.6 feature heads (Ed25519,
HMAC rotation, wrapper coverage) into a single head.

A pre-existing append-only grants gap on `governance_audit_events` is also
closed in this release (CLAUDE.md invariant 2). After upgrading, the
`/health/governance` endpoint should report `append_only_grants_ok: true`.

### ⚠️ Check your monitoring queries

v0.6 renames the halt audit event kind from `agent.killed` to `agent.halted`.
Existing SQL, grep, SIEM, or BI queries filtering on the literal string
`agent.killed` will silently stop matching v0.6 halt events.

**Action required**: audit your downstream consumers. Either:

1. **Use the new view** — `governance_audit_events_halted` unions both kinds:

   ```sql
   SELECT * FROM governance_audit_events_halted
   WHERE created_at > now() - interval '1 hour';
   ```

2. **Or update your queries** to match both kinds:

   ```sql
   WHERE kind IN ('agent.killed', 'agent.halted')
   ```

Historic `agent.killed` rows remain in the chain as-is (HMAC chain integrity
requires append-only). Only new rows after the v0.6 upgrade use `agent.halted`.

**JSON-schema consumers**: v0.6 also adds three new columns to
`governance_audit_events` — `signature`, `signing_key_fingerprint`, and
`signature_status`. Any SIEM, ETL, or BI pipeline that parses audit-event
rows as JSON against a strict schema will see new keys after the upgrade
and may trip schema validation. Either widen the schema to allow the new
keys or filter them out at the export layer before they hit the consumer.

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
- The following v0.6 PRD items remain pending and ship in v0.6.1:
  - **F5** — HITL approval queue panel.
  - **F6 bundle item 3** — audit write rate limiting.
  - **F6 bundle item 6** — tenant-scoped query keys.
  - **`JsonlFallbackStore`** — does not yet round-trip the new
    signature columns; rows recovered from the disk fallback degrade
    to `signature_status='unsigned'` (constraint #7: not a chain
    break).
  - **`vitest.config.ts`** — missing in this cut; the path-alias and
    JSX-from-ts-tests configurations are broken, and the v0.6 a11y
    and v4 unit-test layer is currently pinned via source-grep tests
    only.
  - **No `axe-core`, `@testing-library/react`, or `@playwright/test`
    in `console/package.json` devDependencies** — the a11y guarantees
    are enforced by source-grep assertions until these land.
  - **ESLint config bootstrap** — deferred; `eslint .` is not wired
    into the v0.6 CI matrix.

  F1 (console honesty pass), F2.5 (full `kill`→`halt` rename), F4
  (compliance console surface), and F8 (code quality: TS literal
  narrowing, hand-rolled SSE validator, 4 critical v4 tests) all
  shipped in Wave 4 of v0.6.0 and are NOT pending.
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
