"""Revoke UPDATE/DELETE on governance_audit_events (invariant 2 closure).

Revision ID: 978884c6b7f1
Revises: ab1f55d62f81
Create Date: 2026-04-15 00:00:00.000000

CLAUDE.md architectural invariant #2 states: "Audit writes MUST be
append-only at the DB level (triggers + revoked UPDATE/DELETE grants)."

The ``governance_audit_events`` table has shipped with the append-only
row trigger (``trg_audit_no_update``, ``trg_audit_no_delete``) since
v0.1 — but the role-level REVOKE was never applied in any migration.
``ddl.sql`` line 61 only documents it as something "Operators MUST
also" do. In practice nobody did, and the invariant was half-honored.

The f6a1 migration correctly REVOKEd UPDATE/DELETE on the two new
identity tables (``governance_agent_keys``,
``governance_agent_key_revocations``). That created an asymmetry: new
tables are locked at the grant level, the original audit table is
not. This migration closes that gap and restores the invariant on the
original table.

The trigger remains in place as defense in depth. Customers running a
non-default application role that holds explicit UPDATE/DELETE grants
may need to REVOKE on that role as well — document this in the
operator runbook.

NB: This migration deliberately lives as a clean follow-up to the
``ab1f55d62f81`` merge node (rather than riding inside the merge
migration) because Alembic convention is that merge migrations are
empty-bodied. Schema changes in merge nodes are harder to debug and
easier to lose in a rebase.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "978884c6b7f1"
down_revision: Union[str, Sequence[str], None] = "ab1f55d62f81"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """REVOKE UPDATE, DELETE on governance_audit_events from PUBLIC.

    PostgreSQL grants tables from PUBLIC by default, so revoking from
    PUBLIC is sufficient for SOC 2 alignment on the default role setup.
    Deployments with an explicit application role holding direct grants
    should also REVOKE from that role — see the operator runbook.
    """
    op.execute("REVOKE UPDATE, DELETE ON governance_audit_events FROM PUBLIC")


def downgrade() -> None:
    """Re-grant UPDATE, DELETE on governance_audit_events to PUBLIC.

    The row-level append-only trigger still RAISEs on any UPDATE/DELETE
    attempt, so this downgrade does not materially weaken append-only —
    it only restores the grant state to match pre-migration.
    """
    op.execute("GRANT UPDATE, DELETE ON governance_audit_events TO PUBLIC")
