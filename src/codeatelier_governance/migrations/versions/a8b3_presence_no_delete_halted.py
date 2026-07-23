"""Block DELETE of a halted agent's presence row (self-unhalt defense, part 2).

Revision ID: a8b3nodelhalt
Revises: a7f2haltcols
Create Date: 2026-07-23 00:00:00.000000

SECURITY FIX (self-unhalt-by-close-then-reinsert).

Migration a7f2haltcols moved the operator halt marker into dedicated
columns and revoked the app role's UPDATE on them, closing the
self-unhalt-via-heartbeat vector. But it left a second write path open:
``close_agent()`` issues a ``DELETE`` of the whole presence row, which
removes the halt-marker columns along with it, and the agent's next
heartbeat re-INSERTs the row with ``halted_by = NULL``. A halted agent
could therefore call ``close_agent(self)`` then ``heartbeat(self)`` to
clear its own halt.

Unlike the per-column UPDATE lockdown, DELETE is table-level and cannot be
revoked per column, so this migration enforces the invariant with a
``BEFORE DELETE`` trigger that refuses to delete a row while it carries an
operator halt marker (mirrors the append-only triggers on
governance_audit_events in 978884c6b7f1, but conditional — non-halted rows
still delete so ``close_agent`` works normally). To close a halted agent,
an operator must clear the halt first (a privileged UPDATE the app role
cannot make).
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b3nodelhalt"
down_revision: Union[str, Sequence[str], None] = "a7f2haltcols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Install the conditional no-delete trigger on halted presence rows."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION governance_presence_no_delete_while_halted()
        RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.halted_by IS NOT NULL THEN
                RAISE EXCEPTION 'governance_agent_presence: cannot delete a halted agent (halted_by=%); clear the halt first', OLD.halted_by;
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_presence_no_delete_halted "
        "ON governance_agent_presence"
    )
    op.execute(
        """
        CREATE TRIGGER trg_presence_no_delete_halted
            BEFORE DELETE ON governance_agent_presence
            FOR EACH ROW
            EXECUTE FUNCTION governance_presence_no_delete_while_halted();
        """
    )


def downgrade() -> None:
    """Remove the no-delete trigger and its function."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_presence_no_delete_halted "
        "ON governance_agent_presence"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS governance_presence_no_delete_while_halted()"
    )
