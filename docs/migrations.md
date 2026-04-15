# Running Governance SDK migrations

The Code Atelier Governance SDK uses Alembic to ship schema changes for
v0.6+. You run migrations against the same Postgres the SDK connects
to at runtime.

## Install the migration extra

The SDK's runtime driver is `asyncpg`. Alembic runs in sync mode and
cannot use asyncpg, so migrations need a separate sync driver. We ship
this as an optional extra so customers who never run migrations (e.g.
the SDK is bootstrapped via some other DDL pipeline) do not pay the
wheel-size cost.

```bash
pip install "code-atelier-governance[migrations]"
```

This installs `psycopg[binary]` (psycopg3). We chose psycopg3 over
psycopg2 because it is actively maintained, ships a smaller wheel, and
is the upstream-recommended successor.

## Run the upgrade

```bash
# Point Alembic at your DB. Any of these work:
export SQLALCHEMY_URL="postgresql://user:pass@host:5432/db"
# or use postgresql+asyncpg:// — migrations/env.py rewrites the driver
# automatically to postgresql+psycopg:// at runtime.

# Run all pending migrations up to the single head:
alembic upgrade head
```

`alembic upgrade head` is the canonical command. v0.6 ships a merge
migration (`ab1f55d62f81_merge_v06_feature_heads`) that unifies the
three parallel v0.6 feature branches (Ed25519 agent identity, HMAC
chain key rotation, wrapper coverage registry) into a single head.
Earlier pre-release builds of v0.6 required `alembic upgrade heads`
(plural); this no longer applies.

## What gets applied in v0.6

In order:

1. `f6a1ed25519aid` — Ed25519 agent identity tables
   (`governance_agent_keys`, `governance_agent_key_revocations`) and
   three new columns on `governance_audit_events`
   (`signature`, `signing_key_fingerprint`, `signature_status`).
2. `f6b2_chain_rotation` — HMAC chain key registry
   (`governance_audit_chain_keys`) and the `hmac_next` column on
   `governance_audit_events` for rotation markers.
3. `f9a1b2c3d4e5` — wrapper coverage registry
   (`governance_wrapper_registrations`) with spoofing-prevention
   trigger.
4. `ab1f55d62f81` — empty-bodied merge node joining 1, 2, 3.
5. `978884c6b7f1` — closes invariant #2 by revoking UPDATE/DELETE on
   `governance_audit_events` from PUBLIC (the row-level trigger has
   been in place since v0.1; this migration adds the grant-level
   enforcement).

## Backfill semantics

Existing rows in `governance_audit_events` at the time of the v0.6
upgrade keep `signature_status = 'legacy_unsigned'`. The backfill is
performed via DDL (`ADD COLUMN ... DEFAULT 'legacy_unsigned'`), not
DML (`UPDATE`), because the append-only trigger on
`governance_audit_events` blocks every UPDATE regardless of column.
Legacy rows are NOT a chain break — the verifier accepts
`legacy_unsigned` for rows that predate the Ed25519 rollout.

## Troubleshooting

**"Multiple head revisions are present"** — you are running a
pre-v0.6-final build of the SDK. Upgrade to v0.6 final or later; the
merge migration is included there.

**"ModuleNotFoundError: No module named 'psycopg'"** — you did not
install the `[migrations]` extra. Run
`pip install "code-atelier-governance[migrations]"`.

**"The asyncpg dialect is not implemented for sync use"** — your
`alembic.ini` or environment set a `postgresql+asyncpg://` URL and
the rewrite in `migrations/env.py` did not apply. Make sure you are
using the env.py that ships with v0.6; earlier versions did not
include the driver substitution.
