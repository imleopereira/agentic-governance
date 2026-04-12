"""Add reviewer columns to governance_gates_pending.

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-04-12 00:00:00.000000

Three nullable columns support the reviewer-claim workflow for the
HITL Approval Queue view:

  reviewer_id     -- UUID of the console user who claimed the gate
  reviewing_since -- timestamp when the claim was acquired
  rationale       -- plain-text denial reason (required on deny, optional)

Security notes:
  * reviewer_id references governance_console_users.user_id via application
    logic (no FK to avoid cross-schema coupling in customer deployments).
  * rationale is VARCHAR(2000) to bound payload size; application layer also
    enforces this via Pydantic max_length=2000.
  * All three columns are nullable so existing rows are unaffected.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add reviewer_id, reviewing_since, rationale to governance_gates_pending."""
    op.add_column(
        "governance_gates_pending",
        sa.Column(
            "reviewer_id",
            sa.UUID(),
            nullable=True,
            comment="Console user UUID who has claimed this gate for review",
        ),
    )
    op.add_column(
        "governance_gates_pending",
        sa.Column(
            "reviewing_since",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Timestamp when the claim was acquired; NULL = unclaimed",
        ),
    )
    op.add_column(
        "governance_gates_pending",
        sa.Column(
            "rationale",
            sa.String(2000),
            nullable=True,
            comment="Reviewer rationale; plain text only, never rendered as HTML",
        ),
    )
    # Partial index for stale-claim cleanup queries.
    op.create_index(
        "idx_gates_reviewer",
        "governance_gates_pending",
        ["reviewer_id"],
        postgresql_where=sa.text("reviewer_id IS NOT NULL AND resolved_at IS NULL"),
    )


def downgrade() -> None:
    """Remove reviewer columns from governance_gates_pending."""
    op.drop_index("idx_gates_reviewer", table_name="governance_gates_pending")
    op.drop_column("governance_gates_pending", "rationale")
    op.drop_column("governance_gates_pending", "reviewing_since")
    op.drop_column("governance_gates_pending", "reviewer_id")
