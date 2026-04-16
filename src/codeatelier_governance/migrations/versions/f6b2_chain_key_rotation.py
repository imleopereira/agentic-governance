"""F6 Track B: HMAC chain key rotation — registry + marker column.

Revision ID: f6b2_chain_rotation
Revises: a1b2c3d4e5f6
Create Date: 2026-04-15 00:00:00.000000

Adds:
  1. ``governance_audit_chain_keys`` — append-only registry of chain key
     versions. Holds SALTED fingerprints only, never raw key bytes. Key
     material lives in env vars or 0600 files outside the DB — storing
     it next to the rows it authenticates would create circular trust.

  2. ``governance_audit_events.hmac_next`` — nullable column populated
     ONLY on rotation marker rows (kind = ``audit.chain_key_rotation``),
     carrying the second MAC under the incoming key. Dual-signing is a
     hard requirement: a marker is accepted only if BOTH MACs verify.
     See ``decisions/2026-04-15-f6-hmac-rotation-design.md`` section 5.

Security notes:
  * The fingerprint is HMAC-SHA256(key, "codeatelier.fingerprint.v1") —
    NOT raw sha256(key). The salt defeats offline dictionary attacks
    against weak operator-chosen keys.
  * The chain-keys table is append-only for ``INSERT``. ``UPDATE`` is
    permitted ONLY on ``retired_at_chain_seq`` so rotation can close an
    old window. ``DELETE`` is revoked at the operator level — document
    this in the runbook the same way the audit events grants are
    documented.
  * No FK between the two tables: audit events reference the fingerprint
    via the verifier's in-memory join, not via a constraint. This keeps
    the write path on the audit log completely decoupled from the key
    registry for performance and fault isolation.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6b2_chain_rotation"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create chain-keys registry and add hmac_next column."""
    op.create_table(
        "governance_audit_chain_keys",
        sa.Column("key_version", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "fingerprint",
            sa.Text(),
            nullable=False,
            unique=True,
            comment="HMAC-SHA256(key, 'codeatelier.fingerprint.v1') hex",
        ),
        sa.Column(
            "activated_at_chain_seq",
            sa.BigInteger(),
            nullable=False,
            comment="First chain_seq at which this key authenticates rows",
        ),
        sa.Column(
            "retired_at_chain_seq",
            sa.BigInteger(),
            nullable=True,
            comment="Marker chain_seq at which this key was rotated out; "
            "NULL = currently active",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        comment=(
            "F6 chain key rotation. Append-only registry of HMAC chain key "
            "versions. Stores salted fingerprints only; key material lives "
            "in env vars or files outside the DB (circular-trust avoidance)."
        ),
    )
    op.create_index(
        "ix_chain_keys_active_window",
        "governance_audit_chain_keys",
        ["activated_at_chain_seq"],
    )

    # hmac_next: dual-signed marker second MAC. Nullable because it is
    # populated ONLY on rotation marker rows.
    op.add_column(
        "governance_audit_events",
        sa.Column(
            "hmac_next",
            sa.String(128),
            nullable=True,
            comment="F6 Track B: second HMAC under incoming key, "
            "populated only on rotation marker rows",
        ),
    )


def downgrade() -> None:
    op.drop_column("governance_audit_events", "hmac_next")
    op.drop_index(
        "ix_chain_keys_active_window", table_name="governance_audit_chain_keys"
    )
    op.drop_table("governance_audit_chain_keys")
