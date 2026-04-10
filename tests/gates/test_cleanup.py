"""Tests for the gates retention cleanup helper.

The PostgresGatesStore exposes ``cleanup_resolved(older_than_days)`` so
operators can trim resolved gate rows on their own cadence (cron, etc.).
This test verifies the retention semantics: only resolved rows older than
the threshold are deleted, and pending rows are NEVER touched.

These tests are integration-style and require the QA Postgres at
:5435/governance_qa to be reachable. They are skipped if it isn't.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

try:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from codeatelier_governance.gates.postgres_store import PostgresGatesStore

    _SQLA = True
except ImportError:  # pragma: no cover
    _SQLA = False

DB_URL = os.environ.get(
    "GOVERNANCE_QA_DB_URL",
    "postgresql://governance:governance@localhost:5435/governance_qa",
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _SQLA or not DB_URL.endswith("/governance_qa"),
        reason="QA Postgres not configured",
    ),
]


async def _is_qa_postgres_reachable() -> bool:
    try:
        url = DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_cleanup_only_deletes_old_resolved_rows() -> None:
    if not await _is_qa_postgres_reachable():
        pytest.skip("QA Postgres not reachable")
    url = DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)

    # Set up: insert one OLD-resolved, one NEW-resolved, one pending
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE governance_gates_pending RESTART IDENTITY")
        )
        old_id = uuid4()
        new_id = uuid4()
        pending_id = uuid4()
        # Old resolved (100 days ago)
        await conn.execute(
            text(
                """INSERT INTO governance_gates_pending
                (request_id, agent_id, kind, action_hash, token, expires_at,
                 created_at, resolved_at, resolution)
                VALUES (:rid, 'a', 'k', :ah, :tok,
                        NOW() + INTERVAL '1 day',
                        NOW() - INTERVAL '100 days',
                        NOW() - INTERVAL '100 days', 'granted')"""
            ),
            {
                "rid": str(old_id),
                "ah": "h" * 64,
                "tok": "old-token-" + str(old_id),
            },
        )
        # New resolved (1 day ago)
        await conn.execute(
            text(
                """INSERT INTO governance_gates_pending
                (request_id, agent_id, kind, action_hash, token, expires_at,
                 created_at, resolved_at, resolution)
                VALUES (:rid, 'a', 'k', :ah, :tok,
                        NOW() + INTERVAL '1 day',
                        NOW() - INTERVAL '1 day',
                        NOW() - INTERVAL '1 day', 'granted')"""
            ),
            {
                "rid": str(new_id),
                "ah": "i" * 64,
                "tok": "new-token-" + str(new_id),
            },
        )
        # Still pending — must NEVER be deleted
        await conn.execute(
            text(
                """INSERT INTO governance_gates_pending
                (request_id, agent_id, kind, action_hash, token, expires_at,
                 created_at)
                VALUES (:rid, 'a', 'k', :ah, :tok,
                        NOW() + INTERVAL '1 hour',
                        NOW())"""
            ),
            {
                "rid": str(pending_id),
                "ah": "p" * 64,
                "tok": "pending-token-" + str(pending_id),
            },
        )

    # Run cleanup with 90-day retention
    store = PostgresGatesStore(DB_URL)
    deleted = await store.cleanup_resolved(older_than_days=90)
    assert deleted == 1, f"expected 1 deletion, got {deleted}"
    await store.close()

    # Verify the right rows survived
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT request_id::text FROM governance_gates_pending "
                "ORDER BY request_id"
            )
        )
        remaining = {r[0] for r in res.all()}
    await engine.dispose()

    assert str(old_id) not in remaining, "old resolved row should have been deleted"
    assert str(new_id) in remaining, "recent resolved row must survive"
    assert str(pending_id) in remaining, "pending row must NEVER be deleted by cleanup"


@pytest.mark.asyncio
async def test_cleanup_rejects_negative_days() -> None:
    if not await _is_qa_postgres_reachable():
        pytest.skip("QA Postgres not reachable")
    store = PostgresGatesStore(DB_URL)
    try:
        with pytest.raises(ValueError, match="non-negative"):
            await store.cleanup_resolved(older_than_days=-1)
    finally:
        await store.close()
