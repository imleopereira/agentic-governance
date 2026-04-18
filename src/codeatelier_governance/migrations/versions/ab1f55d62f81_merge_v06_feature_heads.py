"""Merge v0.6 feature heads (f6a1, f6b2, f9a1).

Revision ID: ab1f55d62f81
Revises: f6a1ed25519aid, f6b2_chain_rotation, f9a1b2c3d4e5
Create Date: 2026-04-15 00:00:00.000000

v0.6 ships three independent feature tracks whose migrations all branch
off ``a1b2c3d4e5f6`` in parallel:

  * ``f6a1ed25519aid`` — Ed25519 agent identity
  * ``f6b2_chain_rotation`` — HMAC chain key rotation
  * ``f9a1b2c3d4e5`` — wrapper coverage registry

Without this merge node, ``alembic upgrade head`` (singular) errors out
with "Multiple head revisions are present". Customers expect a single
``head`` to upgrade to. This is an empty-bodied merge migration — all
three feature branches are DDL-independent, so there is nothing to do
here except rejoin the revision graph into a single line.

Schema changes that depend on the merge (e.g. the audit-grant revoke
follow-up) live in their own migration files downstream of this one,
per Alembic convention: merge migrations should be empty.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "ab1f55d62f81"
down_revision: Union[str, Sequence[str], None] = (
    "f6a1ed25519aid",
    "f6b2_chain_rotation",
    "f9a1b2c3d4e5",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Empty merge node — see module docstring."""
    pass


def downgrade() -> None:
    """Empty merge node — see module docstring."""
    pass
