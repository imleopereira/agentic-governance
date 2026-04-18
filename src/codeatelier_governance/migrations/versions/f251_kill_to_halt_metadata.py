"""F2.5: kill → halt view over audit events (no row rewrites).

Revision ID: f251kill2halt
Revises: 978884c6b7f1
Create Date: 2026-04-15 00:00:00.000000

v0.5.4 shipped the operator halt switch under "kill" naming:
``kind='agent.killed'`` in the audit chain and ``_killed_by`` /
``_killed_at`` / ``_kill_reason`` keys inside ``metadata_json`` on
``governance_agent_presence``. v0.6 (F2.5 of the v0.6 PRD) renames the
product-level vocabulary to "halt" across the SDK, console routes,
Pydantic models, structlog events, and the audit ``kind`` field.

HMAC chain integrity (CLAUDE.md architectural invariant #2) says the
audit table is append-only at the DB level: UPDATE and DELETE are
revoked, and a BEFORE UPDATE trigger rejects row mutations. That is
deliberate — the chain hashes each row including its ``kind``, so
rewriting historic ``agent.killed`` rows to ``agent.halted`` would
break the chain link for every event after the first rewrite.
``verify_chain`` would then reject the entire tail.

Therefore this migration does NOT rewrite existing audit rows. It
creates a SQL view that unions the two ``kind`` values so downstream
queries (BI dashboards, compliance exports, the console's events
table) can filter on a single abstract "halt event" concept without
caring which v0.5.x or v0.6 code path originally wrote the row.

Historic ``agent.killed`` rows stay in the chain as-is, forever —
they were HMAC-signed with that kind, and dropping them or rewriting
them would be equivalent to tampering. v0.7 keeps the view unchanged
so that `agent.killed` rows from the v0.5.4 → v0.6 window remain
queryable long after the code path stops producing new ones.

The view is a thin SELECT, not a materialized view: zero storage,
zero background refresh, and every query always sees the latest data.

The presence-table metadata keys (``_killed_by`` vs ``_halted_by``)
live in ``metadata_json`` and are a separate concern — the presence
module reads BOTH key families for v0.6 and drops the fallback in
v0.7. No schema change is needed there because metadata_json is a
jsonb blob.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f251kill2halt"
down_revision: Union[str, Sequence[str], None] = "978884c6b7f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VIEW_NAME = "governance_audit_events_halted"


def upgrade() -> None:
    """Create the halt-union view. Idempotent via CREATE OR REPLACE.

    The view intentionally SELECTs * rather than enumerating columns so
    that future additions to ``governance_audit_events`` (e.g. the
    ``signature`` and ``signing_key_fingerprint`` columns added by the
    f6a1 migration) are automatically visible to view consumers without
    requiring a follow-up migration.
    """
    op.execute(
        f"""
        CREATE OR REPLACE VIEW {_VIEW_NAME} AS
        SELECT *
        FROM governance_audit_events
        WHERE kind IN ('agent.killed', 'agent.halted')
        """
    )


def downgrade() -> None:
    """Drop the halt-union view.

    Downgrade is safe: the view holds no data of its own, and dropping
    it does not touch ``governance_audit_events``. Historic
    ``agent.killed`` rows remain queryable directly on the base table.
    """
    op.execute(f"DROP VIEW IF EXISTS {_VIEW_NAME}")
