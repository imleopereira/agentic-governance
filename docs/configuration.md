# Configuration Reference

This page is the canonical reference for every environment variable read by
the Code Atelier Governance SDK. Each entry lists the variable name, whether
it is required, its default, what it controls, security-relevant notes, and
where it is read in the source tree (file:line) so operators can audit the
call site.

If a variable is not listed here, the SDK does NOT read it. The SDK refuses
to silently consume undocumented environment variables — every config knob
is either a constructor argument on `GovernanceSDK(...)` or one of the
variables below.

---

## Quick reference — minimum production config

The smallest viable production deployment of the SDK requires the
following variables. Everything else has a sensible default.

```bash
# --- SDK (host application) ---
export GOVERNANCE_DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db"
export GOVERNANCE_AUDIT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"

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
| **Read in** | `src/codeatelier_governance/sdk.py:161`, `src/codeatelier_governance/utils.py:28`, `src/codeatelier_governance/cli/commands.py:42` |

PostgreSQL connection string used by the SDK runtime. `postgresql://`,
`postgresql+asyncpg://`, and DSNs with `?sslmode=require` are all accepted;
the SDK normalises the driver scheme internally.

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
`verify_chain(rotation_aware=True)` can resolve and verify rows that were
signed by the older key.

If a row's fingerprint cannot be resolved, the verifier reports
`chain_integrity_status='unverified'` for that row (NOT `corrupt`) and
the fingerprint is added to the bounded LRU `unresolved_fingerprints`
set on the verification result.

**Security**: each value is a cryptographic key. Treat exactly the same
as `GOVERNANCE_AUDIT_SECRET`.

---

## Security notes

The following variables are secrets. They MUST NOT be logged, echoed,
checked into git, or shipped inside container images:

- `GOVERNANCE_DATABASE_URL` (contains DB credentials)
- `GOVERNANCE_AUDIT_SECRET`
- `GOVERNANCE_WORKSPACE_SALT`
- `CODEATELIER_AGENT_KEY_<AGENT_ID>` (every instance)
- `GOVERNANCE_CHAIN_KEY_<FINGERPRINT_PREFIX>` (every instance)

Use a secret manager and inject these at deploy time. The SDK's
`sanitize_db_error()` helper strips the patterns above from any error
message that leaves the process, but you should still treat them as
cryptographically sensitive end-to-end.

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
