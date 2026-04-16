# Configuration Reference

This page is the canonical reference for every environment variable read by
the Code Atelier Governance SDK, the governance console backend, and the
console frontend. Each entry lists the variable name, whether it is required,
its default, what it controls, security-relevant notes, and where it is read
in the source tree (file:line) so operators can audit the call site.

If a variable is not listed here, the SDK does NOT read it. The SDK refuses
to silently consume undocumented environment variables — every config knob
is either a constructor argument on `GovernanceSDK(...)` or one of the
variables below.

---

## Quick reference — minimum production config

The smallest viable production deployment of the SDK + console requires the
following variables. Everything else has a sensible default.

```bash
# --- SDK (host application) ---
export GOVERNANCE_DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db"
export GOVERNANCE_AUDIT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"

# --- Console backend ---
export GOVERNANCE_CONSOLE_HOST="127.0.0.1"   # behind a reverse proxy
export GOVERNANCE_CONSOLE_PORT="8766"
export GOVERNANCE_CONSOLE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export GOVERNANCE_CONSOLE_CORS_ORIGINS="https://console.example.com"

# --- Optional but strongly recommended ---
export GOVERNANCE_WORKSPACE_SALT="$(python -c 'import secrets; print(secrets.token_hex(32))')"

# --- Migrations (one-time, separate venv is fine) ---
pip install "code-atelier-governance[migrations]"
alembic upgrade head
```

Everything in this block should be sourced from a secret manager
(AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault, Doppler) and
injected into the process environment by your deploy tooling. **Never check
these values into git.**

---

## Core SDK variables

These are read by the SDK runtime. They affect every `GovernanceSDK` and
`GovernanceSDKSync` instance in the host application.

### `GOVERNANCE_DATABASE_URL`

| Property | Value |
|---|---|
| **Required** | Yes — at startup if no `database_url=` kwarg is passed |
| **Default** | none (raises `ValueError` if unset and no kwarg) |
| **Read in** | `src/codeatelier_governance/sdk.py:161`, `src/codeatelier_governance/utils.py:28`, `src/codeatelier_governance/cli/commands.py:42`, `src/codeatelier_governance/console/app.py:79`, `src/codeatelier_governance/console/__main__.py:22` |

PostgreSQL connection string used by both the SDK runtime and the console
backend. `postgresql://`, `postgresql+asyncpg://`, and DSNs with
`?sslmode=require` are all accepted; the SDK normalises the driver scheme
internally.

**Security**: this string contains DB credentials. It MUST NOT be logged.
The SDK's `sanitize_db_error()` helper strips it from any error message
that propagates back to the host application.

### `GOVERNANCE_AUDIT_SECRET`

| Property | Value |
|---|---|
| **Required** | No — but strongly recommended in production |
| **Default** | none (an ephemeral 32-byte secret is generated and a WARN is logged) |
| **Read in** | `src/codeatelier_governance/sdk.py:548` |
| **Minimum entropy** | 32 bytes (256 bits); shorter values raise at startup |

HMAC-SHA256 key used to seal every audit-event row to the previous one,
forming the tamper-evident audit chain.

If unset, the SDK generates a fresh per-process key and logs:

```
No GOVERNANCE_AUDIT_SECRET set; generating an ephemeral secret.
Chain integrity will not verify across process restarts.
Set GOVERNANCE_AUDIT_SECRET in production.
```

The chain still works within a single process, but no `verify_chain()`
across process restarts will succeed because the verifier cannot reproduce
the per-process key. **Set this in production.**

**Security**: the value is a cryptographic secret. Never log, never check
into git, never echo to a CI build log. Generate with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### `GOVERNANCE_WORKSPACE_SALT`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | falls back to `GOVERNANCE_AUDIT_SECRET` with a WARN |
| **Read in** | `src/codeatelier_governance/coverage/registry.py:63` |
| **Status** | **New in v0.6** |

HMAC salt used by the F9 wrapper coverage registry to hash the host's
hostname before it is persisted. The hostname is treated as PII and is
never stored in plaintext — only the salted HMAC digest is.

If unset, the registry falls back to `GOVERNANCE_AUDIT_SECRET` and emits
a one-line WARN at first use. Setting a dedicated salt is preferred so
that rotating the audit secret does not change the hostname digests
already persisted in `governance_wrapper_registrations`.

If neither variable is set AND the coverage registry is enabled, the
registry raises at startup. The SDK itself still works — coverage
registration is opt-in via `enable_coverage=True` on `GovernanceConfig`.

**Security**: treat as a secret. Never log.

### `CODEATELIER_TEST_MODE`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | unset (treated as `0`) |
| **Read in** | `src/codeatelier_governance/identity/keystore.py:282` |
| **Status** | **New in v0.6** |

When set to `1`, the identity keystore selects the `EphemeralKeystore`
backend by default. This means agent Ed25519 keys are generated in
memory and discarded on process exit — useful for tests, IPython
shells, and CI smoke runs where you do not want to manage real key
material.

The SDK's pytest plugin sets this automatically when `PYTEST_CURRENT_TEST`
is detected, so unit tests do not need to opt in manually.

**Production note**: do NOT set this in production. Ephemeral keys mean
no signature on an audit row can be verified after the host process
restarts.

### `CODEATELIER_AGENT_KEY_<AGENT_ID>`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | none |
| **Read in** | `src/codeatelier_governance/identity/keystore.py:177` |
| **Status** | **New in v0.6** |

Pattern variable used by the `env://` keystore backend. The suffix is
the agent identifier in upper-snake-case. The value is the agent's
Ed25519 private key, base64-encoded PKCS8.

Example for an agent named `billing-agent`:

```bash
export CODEATELIER_AGENT_KEY_BILLING_AGENT="$(python -c '
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
k = Ed25519PrivateKey.generate()
print(base64.b64encode(k.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)).decode())
')"
```

**Security**: each value is a private signing key. Never log, never
commit, never share between deployments. Use a secret manager. The SDK
zeroes the bytes from memory after they have been parsed.

### `GOVERNANCE_CHAIN_KEY_<FINGERPRINT_PREFIX>`

| Property | Value |
|---|---|
| **Required** | Only when verifying audit rows signed by a rotated-out HMAC chain key |
| **Default** | none |
| **Read in** | `src/codeatelier_governance/audit/keys.py:165` |
| **Status** | **New in v0.6** |

Pattern variable used by the F6 Track B HMAC chain key resolver. The
suffix is the salted fingerprint prefix of the historical key (visible
in the `signing_key_fingerprint` column on `governance_audit_events`).
The value is a base64-encoded 32-byte key.

After rotating the audit chain key with `governance rotate-chain-key`,
operators add one of these env vars per historical key version so that
`verify_chain(rotation_aware=True)` and the `/health/governance`
endpoint can resolve and verify rows that were signed by the older key.

If a row's fingerprint cannot be resolved, the verifier reports
`chain_integrity_status='unverified'` for that row (NOT `corrupt`) and
the fingerprint is added to the bounded LRU `unresolved_fingerprints`
set on the verification result.

**Security**: each value is a cryptographic key. Treat exactly the same
as `GOVERNANCE_AUDIT_SECRET`.

---

## Console backend variables

Read by `python -m codeatelier_governance.console`.

### `GOVERNANCE_CONSOLE_HOST`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `127.0.0.1` |
| **Read in** | `src/codeatelier_governance/console/__main__.py:29` |

Bind address for the FastAPI server. The default is loopback-only —
operators are expected to terminate TLS at a reverse proxy
(nginx, Caddy, ALB) and forward to localhost.

If `GOVERNANCE_CONSOLE_DEV_MODE=true` is set, the host MUST remain
`127.0.0.1` — see the DEV_MODE warning below. v0.6 adds a startup guard
that refuses to launch a non-localhost bind while DEV_MODE is on.

### `GOVERNANCE_CONSOLE_PORT`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `8766` |
| **Read in** | `src/codeatelier_governance/console/__main__.py:30` |

TCP port the console backend listens on.

### `GOVERNANCE_CONSOLE_WORKERS`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `1` |
| **Read in** | `src/codeatelier_governance/console/__main__.py:31` |

Uvicorn worker count. Stay at `1` unless you have measured contention —
the console backend holds a single shared SQLAlchemy engine pool, and
multiple workers will multiply the connection count by the worker count.

### `GOVERNANCE_CONSOLE_LOG_LEVEL`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `info` |
| **Read in** | `src/codeatelier_governance/console/__main__.py:32` |

Uvicorn log level. One of `critical`, `error`, `warning`, `info`,
`debug`, `trace`.

### `GOVERNANCE_CONSOLE_TOKEN`

| Property | Value |
|---|---|
| **Required** | Recommended (or use the session/PBKDF2 user auth path) |
| **Default** | empty string (legacy bearer auth disabled) |
| **Read in** | `src/codeatelier_governance/console/app.py:81` |

Shared bearer token used by the legacy single-token auth path. If set,
clients can authenticate by sending `Authorization: Bearer <token>`.
Most deployments should use the per-user PBKDF2 auth path
(`governance console add-user ...`) instead and leave this unset; the
single-token path is preserved for headless scripts and CI.

**Security**: never log. Generate with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.

### `GOVERNANCE_CONSOLE_DEV_MODE`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `false` |
| **Read in** | `src/codeatelier_governance/console/app.py:82` |

> ## DANGER: do NOT enable in production
>
> When set to `true`, the console backend grants the `admin` role to
> every caller, regardless of whether they presented credentials. This
> exists so that local development against a fresh checkout does not
> need a password. It is a development-only escape hatch.
>
> v0.6 adds a startup guard that refuses to launch with
> `GOVERNANCE_CONSOLE_DEV_MODE=true` unless `GOVERNANCE_CONSOLE_HOST`
> is `127.0.0.1` or `localhost`. A non-localhost bind with DEV_MODE
> enabled now exits non-zero at startup.
>
> Do not set this variable in any environment that is reachable from
> the public internet, from a corporate VPN, or from any host other
> than the operator's local workstation.

### `GOVERNANCE_CONSOLE_ALLOW_DEV_MODE`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `0` |
| **Status** | **v0.6.1 — safety kill-switch** |

Two-key safety lock: even with `GOVERNANCE_CONSOLE_DEV_MODE=true` set,
the dev-mode admin grant only activates if `GOVERNANCE_CONSOLE_ALLOW_DEV_MODE=1`
is also set. The intent is to make accidental DEV_MODE leakage from a
shared `.env` or container image impossible without a second
deliberate flag.

This variable is documented here so that operators can plan for it.
The startup guard ships in v0.6.1 if it is not already present in the
v0.6.0 cut you are running.

### `GOVERNANCE_CONSOLE_SESSION_TTL_HOURS`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `8` |
| **Read in** | `src/codeatelier_governance/console/app.py:83` |

Lifetime in hours of a console login session. After the TTL expires,
the user is re-prompted for credentials.

### `GOVERNANCE_CONSOLE_CORS_ORIGINS`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `http://localhost:3000` |
| **Read in** | `src/codeatelier_governance/console/app.py:85` |

Comma-separated list of origins allowed by the CORS middleware. The
default trusts the local Next.js dev server only; production deployments
must replace it with the public origin of the console UI.

The startup banner emits a warning if the literal `*` is present.

### `GOVERNANCE_CONSOLE_REDACT_KEYS`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | empty |
| **Read in** | `src/codeatelier_governance/console/app.py:91` |

Comma-separated list of additional metadata keys to redact from console
API responses. The console already redacts a built-in list (auth
keywords, OpenAI/Anthropic/Slack/GitHub/AWS key prefixes, DSNs); this
variable lets operators extend it without a code change.

### `GOVERNANCE_CONSOLE_USER_RATE_LIMIT`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `60` (requests per minute per user) |
| **Read in** | `src/codeatelier_governance/console/app.py:139` |
| **Status** | **New in v0.6** |

Per-user request rate limit on the six endpoints wired by F6#4:
`/api/policies`, `/api/policies/{id}`, `/api/events/stats`,
`/api/agents/presence`, `/api/gates/pending`, `/api/gates/recent`.
Returns HTTP 429 with `Retry-After` when exceeded.

### `GOVERNANCE_COMPLIANCE_RATE_LIMIT`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `1` (requests per 60 seconds per user) |
| **Read in** | `src/codeatelier_governance/console/app.py:3116` |
| **Status** | **New in v0.6.0** |

Per-user rate limit on the compliance endpoints (`GET /api/compliance/report`,
`POST /api/compliance/verify-chain`, `POST /api/compliance/export`). Compliance
calls are expensive — they verify the HMAC chain over the requested window
(default last 1000 events) — and should not be treated as cheap polling
endpoints.

The 1-req-per-60-seconds default is deliberately tight. Raise it only for
deployments where an auditor or compliance officer legitimately needs to
export bundles across multiple windows within the rate-limit window.

Returns HTTP 429 with `Retry-After` when exceeded. Rate-limit state is
process-local (in-memory dict keyed on user id) — multi-worker uvicorn
deployments multiply the effective limit by worker count.

---

## Frontend variables

Read by the Next.js console UI in `console/`.

### `NEXT_PUBLIC_CONSOLE_UI_VERSION`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `v3` |
| **Read in** | `console/src/app/(v3)/layout.tsx`, `console/src/components/V3DeprecationBanner.tsx` |
| **Status** | **New in v0.6** |

Selects which IA shell renders. `v3` (default) keeps the v0.5 console
UI; `v4` opts into the new IA shell shipped under
`console/src/app/(v4)/`. The default flips to `v4` once F1 wires the
F3 backend consumers and un-stubs `useAgentPolicy`.

> ## CRITICAL: this is a build-time variable, not runtime
>
> Next.js inlines every `NEXT_PUBLIC_*` variable at `next build` time.
> Setting `NEXT_PUBLIC_CONSOLE_UI_VERSION=v4` in the environment of a
> running container has **no effect** if the container image was built
> without it — the value is already baked into the JavaScript bundle
> shipped to the browser.
>
> **Worked example** — wrong:
>
> ```bash
> docker run -e NEXT_PUBLIC_CONSOLE_UI_VERSION=v4 codeatelier/console:v0.6.0
> # Browser still loads the v3 shell. The env var is ignored.
> ```
>
> **Worked example** — right:
>
> ```bash
> # At build time:
> docker build \
>   --build-arg NEXT_PUBLIC_CONSOLE_UI_VERSION=v4 \
>   -t codeatelier/console:v0.6.0-v4 \
>   .
>
> # At run time:
> docker run codeatelier/console:v0.6.0-v4
> ```
>
> If you ship a single image and need to flip versions per-environment,
> build two images, or use a runtime-injected config endpoint instead
> of a `NEXT_PUBLIC_*` variable.

### `NEXT_PUBLIC_SHOW_V3_BANNER`

| Property | Value |
|---|---|
| **Required** | No |
| **Default** | `0` (banner hidden) |
| **Read in** | `console/src/components/V3DeprecationBanner.tsx` |
| **Status** | **New in v0.6** |

Gate for the v3-deprecation amber banner. Hidden by default because
there is no telemetry pipeline yet and the team review found it was
adding noise to the default v3 experience. Set to `1` locally if you
want to preview the banner. Same `NEXT_PUBLIC_*` build-time caveat as
above applies.

---

## Security notes

The following variables are secrets. They MUST NOT be logged, echoed,
checked into git, or shipped inside container images:

- `GOVERNANCE_DATABASE_URL` (contains DB credentials)
- `GOVERNANCE_AUDIT_SECRET`
- `GOVERNANCE_WORKSPACE_SALT`
- `GOVERNANCE_CONSOLE_TOKEN`
- `CODEATELIER_AGENT_KEY_<AGENT_ID>` (every instance)
- `GOVERNANCE_CHAIN_KEY_<FINGERPRINT_PREFIX>` (every instance)

Use a secret manager and inject these at deploy time. The SDK's
`sanitize_db_error()` and the console's `redact_secrets()` helpers
strip the patterns above from any error or response body that leaves
the process, but you should still treat them as cryptographically
sensitive end-to-end.

The two pattern-variables (`CODEATELIER_AGENT_KEY_*`,
`GOVERNANCE_CHAIN_KEY_*`) are particularly dangerous to log because
naive log redactors that match on exact variable names will miss them.
Audit your log pipeline for prefix-based redaction.

---

## Migration extras

Running `alembic upgrade head` against the SDK schema requires a sync
Postgres driver, which is not part of the runtime install. Add the
`[migrations]` extra:

```bash
pip install "code-atelier-governance[migrations]"
```

This installs `psycopg[binary]` (psycopg3) alongside the runtime
asyncpg driver. See `docs/migrations.md` for the full migration
runbook including dry-run, rollback, and time-estimate guidance.

---

## DEV_MODE warning (recap)

> Setting `GOVERNANCE_CONSOLE_DEV_MODE=true` grants admin to every
> caller. The v0.6 startup guard refuses to launch when DEV_MODE is on
> AND the host bind is anything other than `127.0.0.1`/`localhost`. The
> v0.6.1 follow-up adds `GOVERNANCE_CONSOLE_ALLOW_DEV_MODE=1` as a
> required second flag so accidental leakage from a shared `.env` is
> impossible without two deliberate variables.
>
> If you see DEV_MODE in any production-like environment, treat it as
> a P0 incident: rotate the audit secret, rotate every console user
> credential, audit `governance_audit_events` for the window the flag
> was active, and document the exposure in your incident log.
