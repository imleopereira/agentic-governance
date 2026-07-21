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

The recommended path in v0.6+ is the CLI:

```bash
governance migrate --database-url postgresql://user:pass@host:5432/db
```

This applies the bundled DDL files (base schema) and then runs
`alembic upgrade head` in one shot. Idempotent — running twice is a
no-op because alembic tracks applied revisions in the
`alembic_version` table.

### Manual path (alembic only)

If you already have the base schema in place (e.g. from an earlier
`governance migrate` run or a DBA-managed deployment), you can run
alembic directly:

```bash
# Point Alembic at your DB. Any of these work:
export SQLALCHEMY_URL="postgresql://user:pass@host:5432/db"
# or use postgresql+asyncpg:// — migrations/env.py rewrites the driver
# automatically to postgresql+psycopg:// at runtime.

# Run all pending migrations up to the single head:
alembic upgrade head
```

v0.6 ships a merge migration (`ab1f55d62f81_merge_v06_feature_heads`)
that unifies the three parallel v0.6 feature branches (Ed25519 agent
identity, HMAC chain key rotation, wrapper coverage registry) into a
single head. Earlier pre-release builds of v0.6 required
`alembic upgrade heads` (plural); this no longer applies.

### Why this matters for fresh installs

Prior to v0.6.0 on PyPI, `governance migrate` applied only the DDL
files and left the post-v0.5 schema changes (Ed25519 signing columns,
agent-key tables, rotation markers) to a separate manual
`alembic upgrade head`. Operators who ran `governance migrate` on a
fresh database and did not follow up with alembic ended up on the
v0.5 schema — every `AuditModule.log()` call degraded to
`StoreUnavailableError` against the missing `signature_status`
column and rows were silently dropped per invariant #1. v0.6.0 fixes
this by invoking alembic automatically from `governance migrate`.

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

## Dry-run before applying

Alembic supports a SQL-only "offline" mode that prints the DDL it
would run instead of executing it. Use this against a copy of your
production schema before the real upgrade.

```bash
alembic upgrade head --sql > v0.6-upgrade.sql
less v0.6-upgrade.sql
```

In the generated SQL you will see the `f6a1ed25519aid` step add three
columns to `governance_audit_events`:

```sql
ALTER TABLE governance_audit_events
    ADD COLUMN signature BYTEA;
ALTER TABLE governance_audit_events
    ADD COLUMN signing_key_fingerprint TEXT;
ALTER TABLE governance_audit_events
    ADD COLUMN signature_status TEXT NOT NULL DEFAULT 'legacy_unsigned';
```

This is the DDL-backfill pattern — the `DEFAULT 'legacy_unsigned'`
populates every existing row in a single metadata-only operation
(see "Time estimates" below). Verify the SQL matches the schema you
expect, then apply for real with `alembic upgrade head`.

## Time estimates for the v0.6 upgrade

The v0.6 backfill is intentionally O(1) in row count.

`ADD COLUMN ... DEFAULT 'legacy_unsigned'` on Postgres ≥ 11 is a
metadata-only operation: Postgres records the default in
`pg_attribute.atthasmissing` / `attmissingval` and reads it back at
query time for any row that was inserted before the ALTER. No row is
physically rewritten, no UPDATE is issued, and the table is locked
only for the duration of the catalog change (sub-second on a healthy
cluster).

This means the v0.6 upgrade runs in **seconds even on a
`governance_audit_events` table with 100M+ rows**. You do NOT need a
maintenance window proportional to your audit history. If your DBA's
runbook assumes a row-by-row UPDATE backfill — it does not apply
here.

The trigger-and-grant migrations (`978884c6b7f1`, `f9a1b2c3d4e5`) are
also DDL-only. The merge node (`ab1f55d62f81`) is empty-bodied. The
view migration (`f251kill2halt`) creates a SQL view, which is also
metadata-only.

## Rollback

Each v0.6 migration has a working downgrade path. Use
`alembic downgrade <rev>` where `<rev>` is the revision id you want
to land at, or `alembic downgrade -1` to step back one.

| Migration | Downgrade behaviour |
|---|---|
| `f6a1ed25519aid` | Drops `signature`, `signing_key_fingerprint`, `signature_status` columns and the `governance_agent_keys` / `governance_agent_key_revocations` tables. No UPDATE is issued — the columns simply disappear, so the append-only trigger is not in the way. |
| `f6b2_chain_rotation` | Drops `governance_audit_chain_keys` and the `hmac_next` column. |
| `f9a1b2c3d4e5` | Drops `governance_wrapper_registrations` and its spoofing-prevention trigger. |
| `978884c6b7f1` | Restores `UPDATE, DELETE` grants to PUBLIC on `governance_audit_events`. **Cosmetic only** — the row-level append-only trigger has been in place since v0.1 and continues to block writes regardless of the grant state. |
| `ab1f55d62f81` | No-op (empty merge node). |
| `f251kill2halt` | Drops the `governance_audit_events_halted` view. The underlying `agent.killed` and `agent.halted` rows are untouched. |

After a downgrade, `alembic current` should report the v0.5.x baseline
revision and the SDK should run unchanged.

## Monitoring queries — read this before downgrading SIEM consumers

The v0.6 release renames the halt audit event kind from `agent.killed`
to `agent.halted`. The `governance_audit_events_halted` SQL view
unions both kinds so existing queries can be ported in-place. If you
run downstream SIEM/BI consumers, see the **"Check your monitoring
queries"** section in `CHANGELOG.md` for the worked SQL examples and
the JSON-schema heads-up on the new `signature` /
`signing_key_fingerprint` / `signature_status` columns.

## `SQLALCHEMY_URL` vs `GOVERNANCE_DATABASE_URL`

The two tools use two different conventions, on purpose:

- The runtime SDK reads **`GOVERNANCE_DATABASE_URL`** (see
  `docs/configuration.md`). This is what `GovernanceSDK(...)` uses.
- Alembic reads **`SQLALCHEMY_URL`** via the standard `migrations/env.py`
  pattern, OR the `sqlalchemy.url` value baked into `alembic.ini`. This
  is the upstream alembic convention and we did not invent it.

Two equivalent ways to point alembic at the right database:

```bash
# Option 1: env var, leave alembic.ini untouched.
export SQLALCHEMY_URL="postgresql://user:pass@host:5432/db"
alembic upgrade head

# Option 2: pass the URL on the command line via -x.
alembic -x url="postgresql://user:pass@host:5432/db" upgrade head

# Option 3: edit alembic.ini directly. NOT recommended — tends to
# leak credentials into git.
```

Either `postgresql://` or `postgresql+asyncpg://` works for the
alembic side; `migrations/env.py` rewrites the driver to
`postgresql+psycopg://` at runtime so the same URL the SDK uses can
be reused unchanged.

The two-name split exists because alembic is a separate toolchain
with its own conventions. We chose not to monkey-patch alembic into
reading our env var — that would surprise operators who already know
how alembic works.

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
