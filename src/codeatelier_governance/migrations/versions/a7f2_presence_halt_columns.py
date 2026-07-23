"""Move the operator halt marker into dedicated, revoke-protected columns.

Revision ID: a7f2haltcols
Revises: f251kill2halt
Create Date: 2026-07-23 00:00:00.000000

SECURITY FIX (self-unhalt-by-heartbeat).

Through v0.6 the operator halt marker was stored inside
``governance_agent_presence.metadata_json`` under the ``_halted_*`` (and
legacy ``_killed_*``) keys. But the agent's own heartbeat UPSERT rewrites
``metadata_json`` wholesale on every beat
(``metadata_json = CAST(:meta AS jsonb)``). A halted agent that kept
heartbeating therefore erased the operator's halt marker on its very next
beat — a self-unhalt. The single fail-closed control an operator has over
a running agent could be cleared by the agent it was meant to stop.

This migration moves the marker out of ``metadata_json`` into three
dedicated columns — ``halted_by`` / ``halted_at`` / ``halt_reason`` —
that the heartbeat write path never lists in its column set, then locks
them down at the grant level. It:

  1. adds the three nullable columns (mirrors the a1b2c3d4e5f6 add_column
     pattern);
  2. backfills them one time from any pre-existing ``_halted_*`` /
     ``_killed_*`` metadata so agents halted before the migration stay
     halted through the new read path;
  3. REVOKEs table-wide UPDATE from PUBLIC and re-GRANTs UPDATE only on
     the four columns the heartbeat UPSERT actually sets (status,
     last_heartbeat, metadata_json, operator_id), leaving the halt-marker
     columns unwritable by the app/agent role. This mirrors the
     append-only REVOKE pattern applied to governance_audit_events in
     978884c6b7f1 and turns "the heartbeat cannot clear a halt" from a
     code convention into a DB-enforced guarantee.

Deployments using an explicit application role that holds direct grants
must ALSO REVOKE UPDATE on halted_by/halted_at/halt_reason from that role
— document this in the operator runbook (same caveat as 978884c6b7f1).
The privileged console/halt role retains table-wide UPDATE and remains
the only writer of the halt marker.

The presence module keeps reading the legacy ``_halted_*`` / ``_killed_*``
metadata keys as a COALESCE fallback for one release; v0.7 drops it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7f2haltcols"
down_revision: Union[str, Sequence[str], None] = "f251kill2halt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add halt-marker columns, backfill from metadata, and lock them down."""
    op.add_column(
        "governance_agent_presence",
        sa.Column(
            "halted_by",
            sa.String(256),
            nullable=True,
            comment="Operator identity that halted the agent; NULL = not halted",
        ),
    )
    op.add_column(
        "governance_agent_presence",
        sa.Column(
            "halted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Timestamp the halt marker was written; NULL = not halted",
        ),
    )
    op.add_column(
        "governance_agent_presence",
        sa.Column(
            "halt_reason",
            sa.String(2000),
            nullable=True,
            comment="Free-text halt reason; plain text only, never rendered as HTML",
        ),
    )

    # One-time backfill so agents halted before this migration stay halted
    # via the new columns. Reads both the v0.6 (_halted_*) and v0.5.x
    # (_killed_*) metadata key families.
    op.execute(
        """
        UPDATE governance_agent_presence
        SET halted_by = COALESCE(metadata_json->>'_halted_by',
                                 metadata_json->>'_killed_by'),
            halted_at = COALESCE((metadata_json->>'_halted_at')::timestamptz,
                                 (metadata_json->>'_killed_at')::timestamptz),
            halt_reason = COALESCE(metadata_json->>'_halt_reason',
                                   metadata_json->>'_kill_reason')
        WHERE metadata_json->>'_halted_by' IS NOT NULL
           OR metadata_json->>'_killed_by' IS NOT NULL
        """
    )

    # Column-level grant lock-down: PUBLIC keeps UPDATE only on the four
    # columns the heartbeat UPSERT sets, so the app/agent role cannot
    # overwrite the halt marker. See module docstring and ddl.sql.
    op.execute("REVOKE UPDATE ON governance_agent_presence FROM PUBLIC")
    op.execute(
        "GRANT UPDATE (status, last_heartbeat, metadata_json, operator_id) "
        "ON governance_agent_presence TO PUBLIC"
    )


def downgrade() -> None:
    """Restore table-wide UPDATE and drop the halt-marker columns.

    Restoring the grant before dropping the columns keeps the grant state
    consistent with a pre-migration table (table-wide UPDATE to PUBLIC).
    Any live halt markers are lost on downgrade — operators must re-halt
    from the console, which will again write the metadata keys the older
    code path reads.
    """
    op.execute("REVOKE UPDATE ON governance_agent_presence FROM PUBLIC")
    op.execute("GRANT UPDATE ON governance_agent_presence TO PUBLIC")
    op.drop_column("governance_agent_presence", "halt_reason")
    op.drop_column("governance_agent_presence", "halted_at")
    op.drop_column("governance_agent_presence", "halted_by")
