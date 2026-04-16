"""F6 Track A: Ed25519 agent identity tables + audit row columns.

Revision ID: f6a1ed25519aid
Revises: a1b2c3d4e5f6
Create Date: 2026-04-15 00:00:00.000000

Adds:
  * ``governance_agent_keys`` — append-only public key registry.
  * ``governance_agent_key_revocations`` — append-only revocation log.
  * Three columns on ``governance_audit_events``:
      - ``signature BYTEA NULL``
      - ``signing_key_fingerprint TEXT NULL``
      - ``signature_status TEXT NOT NULL DEFAULT 'unsigned'``

Append-only enforcement is at the DB grant level: UPDATE and DELETE are
REVOKEd from PUBLIC on both new tables.  This matches the existing audit
events append-only convention.

Backfill: existing ``governance_audit_events`` rows predate Ed25519
signing, so their ``signature_status`` must land as ``'legacy_unsigned'``.
**This cannot be done with an UPDATE** — ``governance_audit_events`` has
a row-level append-only trigger that blocks every UPDATE regardless of
column.  Instead we use a DDL-only backfill: ``ADD COLUMN ... DEFAULT
'legacy_unsigned'`` (Postgres writes the default into every existing
row as part of the DDL itself, without firing row triggers), then
``ALTER COLUMN ... SET DEFAULT 'unsigned'`` so future INSERTs get the
live default.  Constraint #7 of the design: legacy rows are NOT a chain
break.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a1ed25519aid"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- governance_agent_keys -------------------------------------------
    op.create_table(
        "governance_agent_keys",
        sa.Column("key_fingerprint", sa.Text(), primary_key=True),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("activated_at_chain_seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        comment=(
            "F6 Ed25519 agent identity. Append-only. Public keys only — "
            "private keys live in env/file outside DB."
        ),
    )
    op.create_index(
        "ix_agent_keys_agent",
        "governance_agent_keys",
        ["agent_id"],
    )

    # --- governance_agent_key_revocations --------------------------------
    op.create_table(
        "governance_agent_key_revocations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("key_fingerprint", sa.Text(), nullable=False),
        sa.Column("revoked_at_chain_seq", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "key_fingerprint",
            "revoked_at_chain_seq",
            name="uq_revocation_fp_seq",
        ),
        comment=(
            "F6 Ed25519 key revocations. Append-only. Verifier rejects "
            "signatures from fingerprints revoked before the row chain_seq."
        ),
    )
    op.create_index(
        "ix_revocations_fp",
        "governance_agent_key_revocations",
        ["key_fingerprint"],
    )

    # --- Append-only enforcement -----------------------------------------
    # Revoke UPDATE/DELETE from PUBLIC on both tables. Same pattern used
    # for the existing governance_audit_events table.
    op.execute(
        "REVOKE UPDATE, DELETE ON governance_agent_keys FROM PUBLIC"
    )
    op.execute(
        "REVOKE UPDATE, DELETE ON governance_agent_key_revocations FROM PUBLIC"
    )

    # --- governance_audit_events columns ---------------------------------
    op.add_column(
        "governance_audit_events",
        sa.Column(
            "signature",
            sa.LargeBinary(),
            nullable=True,
            comment="Raw 64-byte Ed25519 signature over canonical row bytes",
        ),
    )
    op.add_column(
        "governance_audit_events",
        sa.Column(
            "signing_key_fingerprint",
            sa.Text(),
            nullable=True,
            comment=(
                "Fingerprint of the Ed25519 key that signed this row. "
                "References governance_agent_keys.key_fingerprint in-window "
                "but NOT an FK — registry can outlive revoked keys."
            ),
        ),
    )
    # !!! DO NOT "SIMPLIFY" THIS INTO AN UPDATE !!!
    # DDL-only backfill via column DEFAULT — UPDATE is forbidden by the
    # append-only trigger on governance_audit_events (ddl.sql installs
    # trg_audit_no_update which RAISEs on every UPDATE regardless of
    # column). Postgres ADD COLUMN with DEFAULT performs an in-place
    # backfill of existing rows as part of the DDL itself, without
    # firing row triggers. Then ALTER COLUMN SET DEFAULT switches
    # future INSERTs to 'unsigned'. Existing rows keep 'legacy_unsigned';
    # new rows start as 'unsigned' until the signer path overwrites
    # them with 'signed' or 'unsigned_local_failure'.
    #
    # Regression history: the first draft of this migration used an
    # UPDATE and crashed against a real Postgres with 243 existing
    # audit rows (v0.6 pre-release, 2026-04-15). Tests passed because
    # the test DB was empty. The integration test at
    # tests/integration/test_migration_against_seeded_db.py now seeds
    # real rows before running `alembic upgrade head` to catch any
    # future regression.
    op.add_column(
        "governance_audit_events",
        sa.Column(
            "signature_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'legacy_unsigned'"),
            comment=(
                "Allowed values: signed | unsigned | unsigned_local_failure "
                "| legacy_unsigned | revoked_key | invalid_signature | unknown_key"
            ),
        ),
    )
    op.alter_column(
        "governance_audit_events",
        "signature_status",
        server_default=sa.text("'unsigned'"),
    )


def downgrade() -> None:
    op.drop_column("governance_audit_events", "signature_status")
    op.drop_column("governance_audit_events", "signing_key_fingerprint")
    op.drop_column("governance_audit_events", "signature")
    op.drop_index(
        "ix_revocations_fp", table_name="governance_agent_key_revocations"
    )
    op.drop_table("governance_agent_key_revocations")
    op.drop_index("ix_agent_keys_agent", table_name="governance_agent_keys")
    op.drop_table("governance_agent_keys")
