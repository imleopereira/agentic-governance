"""CLI command implementations using stdlib argparse.

Commands:
    governance migrate   — apply DDL to a fresh Postgres
    governance verify    — walk the HMAC chain for a session and assert integrity
    governance tail      — live-follow audit events (poll every 1s)
    governance budget    — show current cost snapshot for an agent

Security: the CLI never prints the audit secret, even with --verbose.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

# DDL files shipped with the SDK.
_PKG_ROOT = Path(__file__).resolve().parent.parent
_DDL_FILES = [
    _PKG_ROOT / "audit" / "ddl.sql",
    _PKG_ROOT / "cost" / "ddl.sql",
    _PKG_ROOT / "gates" / "ddl.sql",
]


def _resolve_database_url(args: argparse.Namespace) -> str:
    """Resolve --database-url from args or GOVERNANCE_DATABASE_URL env var."""
    url: str | None = getattr(args, "database_url", None)
    if not url:
        url = os.environ.get("GOVERNANCE_DATABASE_URL")
    if not url:
        sys.stderr.write(
            "Error: --database-url is required (or set GOVERNANCE_DATABASE_URL).\n"
        )
        sys.exit(2)
    return url


def _normalize_url_sync(url: str) -> str:
    """Convert postgresql:// to postgresql+asyncpg:// for SQLAlchemy async."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


async def _run_migrate(database_url: str) -> None:
    """Apply all DDL files to the database. Idempotent (IF NOT EXISTS)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_normalize_url_sync(database_url))
    try:
        for ddl_path in _DDL_FILES:
            if not ddl_path.exists():
                sys.stderr.write(f"Warning: DDL file not found: {ddl_path}\n")
                continue
            ddl_sql = ddl_path.read_text()
            async with engine.begin() as conn:
                # Execute each statement separately for Postgres compatibility.
                # The DDL files use IF NOT EXISTS so this is idempotent.
                from sqlalchemy import text

                await conn.execute(text(ddl_sql))
            sys.stdout.write(f"Applied: {ddl_path.name}\n")
    finally:
        await engine.dispose()
    sys.stdout.write("Migration complete.\n")


async def _run_verify(database_url: str, session_id: UUID) -> int:
    """Walk the HMAC chain for a session. Returns exit code 0 (clean) or 1 (tampered)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from codeatelier_governance.audit.chain import verify_event
    from codeatelier_governance.audit.models import AuditEventRecord

    secret_str = os.environ.get("GOVERNANCE_AUDIT_SECRET")
    if not secret_str:
        sys.stderr.write(
            "Error: GOVERNANCE_AUDIT_SECRET env var is required for verification.\n"
        )
        return 2

    secret = secret_str.encode("utf-8")
    engine = create_async_engine(_normalize_url_sync(database_url))

    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT event_id, session_id, agent_id, parent_event_id, "
                    "kind, input_hash, output_hash, metadata_json, "
                    "prev_hash, hmac_value, created_at "
                    "FROM governance_audit_events "
                    "WHERE session_id = :sid "
                    "ORDER BY chain_seq"
                ),
                {"sid": str(session_id)},
            )
            rows = list(result.mappings())

        if not rows:
            sys.stderr.write(f"No events found for session {session_id}.\n")
            return 1

        for row in rows:
            record = AuditEventRecord(
                event_id=row["event_id"],
                session_id=row["session_id"],
                agent_id=row["agent_id"],
                parent_event_id=row["parent_event_id"],
                kind=row["kind"],
                input_hash=row["input_hash"],
                output_hash=row["output_hash"],
                metadata=row["metadata_json"],
                prev_hash=row["prev_hash"],
                hmac=row["hmac_value"],
                created_at=row["created_at"],
            )
            if not verify_event(record, secret):
                sys.stdout.write(f"TAMPERED: {record.event_id}\n")
                return 1

        sys.stdout.write(
            f"OK: {len(rows)} events verified for session {session_id}.\n"
        )
        return 0
    finally:
        await engine.dispose()


async def _run_tail(
    database_url: str,
    agent_id: str | None,
    kind: str | None,
) -> None:
    """Live-follow audit events. Polls every 1s, prints each new event as JSON."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_normalize_url_sync(database_url))
    last_seq = 0

    try:
        while True:
            where_clauses = ["chain_seq > :last_seq"]
            params: dict[str, Any] = {"last_seq": last_seq}

            if agent_id:
                where_clauses.append("agent_id = :agent_id")
                params["agent_id"] = agent_id
            if kind:
                where_clauses.append("kind = :kind")
                params["kind"] = kind

            where = " AND ".join(where_clauses)
            query = (
                f"SELECT chain_seq, event_id, session_id, agent_id, kind, "
                f"metadata_json, created_at "
                f"FROM governance_audit_events "
                f"WHERE {where} "
                f"ORDER BY chain_seq "
                f"LIMIT 100"
            )

            async with engine.connect() as conn:
                result = await conn.execute(text(query), params)
                rows = list(result.mappings())

            for row in rows:
                event_data = {
                    "chain_seq": row["chain_seq"],
                    "event_id": str(row["event_id"]),
                    "session_id": str(row["session_id"]),
                    "agent_id": row["agent_id"],
                    "kind": row["kind"],
                    "metadata": row["metadata_json"],
                    "created_at": row["created_at"].isoformat(),
                }
                sys.stdout.write(json.dumps(event_data, default=str) + "\n")
                sys.stdout.flush()
                last_seq = row["chain_seq"]

            await asyncio.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        await engine.dispose()


async def _run_budget(database_url: str, agent_id: str) -> None:
    """Show current cost snapshot for an agent."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_normalize_url_sync(database_url))
    try:
        async with engine.connect() as conn:
            # Session usage (all sessions for this agent)
            result = await conn.execute(
                text(
                    "SELECT session_id, usd_used, tokens_used, last_updated "
                    "FROM governance_cost_session_usage "
                    "WHERE agent_id = :agent_id "
                    "ORDER BY last_updated DESC"
                ),
                {"agent_id": agent_id},
            )
            sessions = list(result.mappings())

            # Daily usage
            result = await conn.execute(
                text(
                    "SELECT day_utc, usd_used, tokens_used "
                    "FROM governance_cost_agent_daily "
                    "WHERE agent_id = :agent_id "
                    "ORDER BY day_utc DESC "
                    "LIMIT 7"
                ),
                {"agent_id": agent_id},
            )
            daily = list(result.mappings())

        output: dict[str, Any] = {
            "agent_id": agent_id,
            "sessions": [
                {
                    "session_id": str(s["session_id"]),
                    "usd_used": float(s["usd_used"]),
                    "tokens_used": int(s["tokens_used"]),
                    "last_updated": s["last_updated"].isoformat(),
                }
                for s in sessions
            ],
            "daily": [
                {
                    "day_utc": str(d["day_utc"]),
                    "usd_used": float(d["usd_used"]),
                    "tokens_used": int(d["tokens_used"]),
                }
                for d in daily
            ],
        }
        sys.stdout.write(json.dumps(output, indent=2, default=str) + "\n")
    finally:
        await engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="governance",
        description="Code Atelier Governance SDK CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # migrate
    migrate_parser = subparsers.add_parser(
        "migrate", help="Apply DDL migrations to a fresh Postgres database"
    )
    migrate_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )

    # verify
    verify_parser = subparsers.add_parser(
        "verify", help="Walk the HMAC chain for a session and assert integrity"
    )
    verify_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )
    verify_parser.add_argument(
        "--session-id", type=str, required=True,
        help="UUID of the session to verify",
    )

    # tail
    tail_parser = subparsers.add_parser(
        "tail", help="Live-follow audit events from Postgres"
    )
    tail_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )
    tail_parser.add_argument(
        "--agent-id", type=str, default=None,
        help="Filter by agent ID",
    )
    tail_parser.add_argument(
        "--kind", type=str, default=None,
        help="Filter by event kind",
    )

    # budget
    budget_parser = subparsers.add_parser(
        "budget", help="Show current cost snapshot for an agent"
    )
    budget_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )
    budget_parser.add_argument(
        "--agent-id", type=str, required=True,
        help="Agent ID to inspect",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "migrate":
        database_url = _resolve_database_url(args)
        asyncio.run(_run_migrate(database_url))

    elif args.command == "verify":
        database_url = _resolve_database_url(args)
        session_id = UUID(args.session_id)
        exit_code = asyncio.run(_run_verify(database_url, session_id))
        sys.exit(exit_code)

    elif args.command == "tail":
        database_url = _resolve_database_url(args)
        asyncio.run(_run_tail(database_url, args.agent_id, args.kind))

    elif args.command == "budget":
        database_url = _resolve_database_url(args)
        asyncio.run(_run_budget(database_url, args.agent_id))

    else:
        parser.print_help()
        sys.exit(1)
