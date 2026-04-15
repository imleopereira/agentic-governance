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
    _PKG_ROOT / "console" / "ddl.sql",
    _PKG_ROOT / "loop" / "ddl.sql",
    _PKG_ROOT / "presence" / "ddl.sql",
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


def _run_migrate_dry_run() -> None:
    """Print all DDL that would be executed, without touching the database."""
    for ddl_path in _DDL_FILES:
        if not ddl_path.exists():
            sys.stderr.write(f"Warning: DDL file not found: {ddl_path.name}\n")
            continue
        sys.stdout.write(f"-- {ddl_path.name}\n")
        sys.stdout.write(ddl_path.read_text())
        sys.stdout.write("\n\n")
    sys.stdout.write("-- Dry run complete. No changes applied.\n")


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL file into individual statements for asyncpg.

    Handles PL/pgSQL function bodies delimited by $$ ... $$ by not
    splitting on semicolons inside dollar-quoted strings.
    """
    statements: list[str] = []
    current: list[str] = []
    in_dollar_quote = False

    for line in sql.split("\n"):
        stripped = line.strip()
        # Skip pure comment lines (but keep comments within statements)
        if not current and (not stripped or stripped.startswith("--")):
            continue

        # Track $$ delimiters for PL/pgSQL blocks
        dollar_count = line.count("$$")
        if dollar_count % 2 == 1:
            in_dollar_quote = not in_dollar_quote

        current.append(line)

        # Statement ends at semicolon OUTSIDE dollar-quoted blocks
        if stripped.endswith(";") and not in_dollar_quote:
            stmt = "\n".join(current).strip()
            if stmt and not all(
                ln.strip().startswith("--") or not ln.strip()
                for ln in stmt.split("\n")
            ):
                statements.append(stmt)
            current = []

    # Handle any trailing statement without semicolon
    if current:
        stmt = "\n".join(current).strip()
        if stmt and not all(
            ln.strip().startswith("--") or not ln.strip()
            for ln in stmt.split("\n")
        ):
            statements.append(stmt)

    return statements


async def _run_migrate(database_url: str) -> None:
    """Apply all DDL files to the database. Idempotent (IF NOT EXISTS)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_normalize_url_sync(database_url))
    try:
        for ddl_path in _DDL_FILES:
            if not ddl_path.exists():
                sys.stderr.write(f"Warning: DDL file not found: {ddl_path.name}\n")
                continue
            ddl_sql = ddl_path.read_text()
            from sqlalchemy import text

            # asyncpg cannot execute multiple statements in a single
            # prepared statement. Split on semicolons, but preserve
            # PL/pgSQL function bodies (delimited by $$..$$).
            statements = _split_sql_statements(ddl_sql)
            async with engine.begin() as conn:
                for stmt in statements:
                    await conn.execute(text(stmt))
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


async def _run_rotate_chain_key(
    *,
    database_url: str,
    new_key_uri: str,
    operator_id: str,
    reason: str,
) -> None:
    """Execute an HMAC chain key rotation via the rotation.py procedure.

    Resolves the outgoing key from ``GOVERNANCE_AUDIT_SECRET`` and the
    incoming key from ``--new-key-uri``. Prints a prominent warning
    telling the operator which env var every verifier instance must be
    updated to set — see design doc section 14.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from codeatelier_governance.audit.keys import _resolve_uri  # type: ignore
    from codeatelier_governance.audit.rotation import rotate_chain_key

    outgoing = os.environ.get("GOVERNANCE_AUDIT_SECRET")
    if not outgoing:
        sys.stderr.write(
            "Error: GOVERNANCE_AUDIT_SECRET must be set to the CURRENT "
            "(outgoing) key before rotating.\n"
        )
        sys.exit(2)
    outgoing_bytes = outgoing.encode("utf-8")

    incoming_bytes = _resolve_uri(new_key_uri)
    if incoming_bytes is None:
        sys.stderr.write(
            f"Error: could not resolve --new-key-uri {new_key_uri!r}. "
            f"Check that the env var is set or the file exists and is readable.\n"
        )
        sys.exit(2)

    engine = create_async_engine(_normalize_url_sync(database_url))
    try:
        result = await rotate_chain_key(
            engine,
            outgoing_secret=outgoing_bytes,
            incoming_secret=incoming_bytes,
            operator_id=operator_id,
            rotation_reason=reason,
        )
    finally:
        await engine.dispose()

    # Flush the bounded LRU negative-result cache so the verifier picks
    # up the freshly-provisioned incoming key on the next ``resolve_key``
    # call. Without this, an operator who ran a verify BEFORE rotating
    # (seeding the cache with ``UNAVAILABLE`` for the new fingerprint)
    # would continue seeing ``unverified`` rows until the process
    # restarted. POLISH 2 DA fix.
    from codeatelier_governance.audit.keys import clear_key_cache
    clear_key_cache()

    sys.stdout.write(
        "Chain key rotation complete.\n"
        f"  marker chain_seq : {result.marker_chain_seq}\n"
        f"  outgoing version : {result.outgoing_key_version}\n"
        f"  incoming version : {result.incoming_key_version}\n"
        f"  outgoing fp      : {result.outgoing_fingerprint[:16]}...\n"
        f"  incoming fp      : {result.incoming_fingerprint[:16]}...\n"
        "\n"
        "ACTION REQUIRED: every verifier instance must be configured to "
        "resolve the incoming fingerprint via a URI map entry pointing at "
        f"the same key bytes used above ({new_key_uri}). Until this is done, "
        "rows past the marker will verify as 'unverified' (not 'failed').\n"
    )


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
    migrate_parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print the DDL that would be executed without applying it",
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

    # report
    report_parser = subparsers.add_parser(
        "report", help="Generate Article 12 evidence reports from audit trail data"
    )
    report_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )
    report_parser.add_argument(
        "--session-id", type=str, default=None,
        help="UUID of a specific session to report on",
    )
    report_parser.add_argument(
        "--agent-id", type=str, default=None,
        help="Agent ID to filter by",
    )
    report_parser.add_argument(
        "--from", type=str, default=None, dest="date_from",
        help="Start date for report range (ISO 8601, e.g. 2026-04-01)",
    )
    report_parser.add_argument(
        "--to", type=str, default=None, dest="date_to",
        help="End date for report range (ISO 8601, e.g. 2026-04-10)",
    )
    report_parser.add_argument(
        "--format", type=str, default="article12",
        choices=["article12", "summary"], dest="report_format",
        help="Report format (default: article12)",
    )
    report_parser.add_argument(
        "--output", type=str, default=None,
        help="Output file path (default: stdout)",
    )

    # rotate-chain-key (F6 Track B)
    rotate_parser = subparsers.add_parser(
        "rotate-chain-key",
        help="Rotate the HMAC chain key (F6 Track B: dual-signed marker row)",
    )
    rotate_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )
    rotate_parser.add_argument(
        "--new-key-uri", type=str, required=True,
        help="URI of the incoming key material: env://NAME or file:///path",
    )
    rotate_parser.add_argument(
        "--operator-id", type=str, default="cli",
        help="Operator identifier recorded in marker metadata",
    )
    rotate_parser.add_argument(
        "--reason", type=str, default="scheduled",
        choices=["scheduled", "incident", "policy"],
        help="Rotation reason (default: scheduled)",
    )
    rotate_parser.add_argument(
        "--confirm", action="store_true", default=False,
        help="Confirm the rotation (required — prevents accidental rotations)",
    )

    # console (user management subcommands)
    console_parser = subparsers.add_parser(
        "console", help="Console user management commands"
    )
    console_sub = console_parser.add_subparsers(
        dest="console_command", help="Console subcommands"
    )

    # console add-user
    add_user_parser = console_sub.add_parser(
        "add-user", help="Create a console user"
    )
    add_user_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )
    add_user_parser.add_argument(
        "--username", type=str, required=True, help="Username"
    )
    add_user_parser.add_argument(
        "--role", type=str, default="viewer", choices=["viewer", "admin"],
        help="User role (default: viewer)",
    )
    add_user_parser.add_argument(
        "--password", type=str, default=None,
        help="Password (prompted interactively if not provided)",
    )

    # console list-users
    list_users_parser = console_sub.add_parser(
        "list-users", help="List all console users"
    )
    list_users_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )

    # console disable-user
    disable_user_parser = console_sub.add_parser(
        "disable-user", help="Disable a console user"
    )
    disable_user_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )
    disable_user_parser.add_argument(
        "--username", type=str, required=True, help="Username to disable"
    )

    # console reset-password
    reset_pw_parser = console_sub.add_parser(
        "reset-password", help="Reset a user's password"
    )
    reset_pw_parser.add_argument(
        "--database-url", type=str, default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )
    reset_pw_parser.add_argument(
        "--username", type=str, required=True, help="Username"
    )
    reset_pw_parser.add_argument(
        "--password", type=str, default=None,
        help="New password (prompted interactively if not provided)",
    )

    return parser


async def _run_report(
    database_url: str,
    report_format: str,
    session_id: str | None,
    agent_id: str | None,
    date_from_str: str | None,
    date_to_str: str | None,
    output_path: str | None,
) -> None:
    """Generate an Article 12 evidence report and write JSON to output."""
    from datetime import datetime as dt
    from datetime import timezone

    from codeatelier_governance.compliance.report import ReportGenerator

    # Normalize URL for async
    url = _normalize_url_sync(database_url)
    generator = ReportGenerator(database_url=url)

    # Parse dates
    date_from: dt | None = None
    date_to: dt | None = None
    if date_from_str:
        date_from = dt.fromisoformat(date_from_str)
        if date_from.tzinfo is None:
            date_from = date_from.replace(tzinfo=timezone.utc)
    if date_to_str:
        date_to = dt.fromisoformat(date_to_str)
        if date_to.tzinfo is None:
            date_to = date_to.replace(tzinfo=timezone.utc)

    # Parse session IDs
    session_ids: list[UUID] | None = None
    if session_id:
        try:
            session_ids = [UUID(session_id)]
        except ValueError:
            sys.stderr.write(f"Invalid session-id: {session_id}\n")
            sys.exit(1)

    if report_format == "summary":
        if not agent_id:
            sys.stderr.write("Error: --agent-id is required for summary format.\n")
            sys.exit(2)
        report = await generator.generate_summary(
            agent_id=agent_id,
            date_from=date_from,
            date_to=date_to,
        )
    else:
        report = await generator.generate_article12(
            session_ids=session_ids,
            agent_id=agent_id,
            date_from=date_from,
            date_to=date_to,
        )

    report_json = report.model_dump_json(indent=2)

    if output_path:
        Path(output_path).write_text(report_json)
        sys.stdout.write(f"Report written to {output_path}\n")
    else:
        sys.stdout.write(report_json + "\n")


async def _run_console_add_user(
    database_url: str, username: str, role: str, password: str
) -> None:
    """Create a console user in Postgres."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from codeatelier_governance.console.auth import hash_password

    engine = create_async_engine(_normalize_url_sync(database_url))
    try:
        uid = str(__import__("uuid").uuid4())
        pw_hash = hash_password(password)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO governance_console_users "
                    "(user_id, username, password_hash, role, created_at, updated_at) "
                    "VALUES (:uid, :username, :pw_hash, :role, NOW(), NOW())"
                ),
                {
                    "uid": uid,
                    "username": username.lower(),
                    "pw_hash": pw_hash,
                    "role": role,
                },
            )
        sys.stdout.write(f"Created user '{username}' with role '{role}'.\n")
    except Exception as exc:
        from sqlalchemy.exc import IntegrityError
        if isinstance(exc, IntegrityError):
            sys.stderr.write(f"Error: username '{username}' already exists.\n")
            sys.exit(1)
        logger.error(
            "cli.add_user_failed",
            error_type=type(exc).__name__,
        )
        sys.stderr.write(f"Error: failed to create user ({type(exc).__name__}). Check database connectivity.\n")
        sys.exit(1)
    finally:
        await engine.dispose()


async def _run_console_list_users(database_url: str) -> None:
    """List all console users."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_normalize_url_sync(database_url))
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT username, role, disabled, created_at "
                    "FROM governance_console_users ORDER BY created_at"
                )
            )
            rows = list(res.mappings())
        if not rows:
            sys.stdout.write("No users found.\n")
            return
        for row in rows:
            status = "disabled" if row["disabled"] else "active"
            sys.stdout.write(
                f"  {row['username']:20s}  {row['role']:8s}  {status:10s}  "
                f"{row['created_at'].isoformat()}\n"
            )
    finally:
        await engine.dispose()


async def _run_console_disable_user(database_url: str, username: str) -> None:
    """Disable a console user."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_normalize_url_sync(database_url))
    try:
        async with engine.begin() as conn:
            res = await conn.execute(
                text(
                    "UPDATE governance_console_users "
                    "SET disabled = TRUE, updated_at = NOW() "
                    "WHERE username = :username"
                ),
                {"username": username.lower()},
            )
            if res.rowcount == 0:
                sys.stderr.write(f"Error: user '{username}' not found.\n")
                sys.exit(1)
        sys.stdout.write(f"Disabled user '{username}'.\n")
    finally:
        await engine.dispose()


async def _run_console_reset_password(
    database_url: str, username: str, password: str
) -> None:
    """Reset a user's password."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from codeatelier_governance.console.auth import hash_password

    engine = create_async_engine(_normalize_url_sync(database_url))
    try:
        pw_hash = hash_password(password)
        async with engine.begin() as conn:
            res = await conn.execute(
                text(
                    "UPDATE governance_console_users "
                    "SET password_hash = :pw_hash, updated_at = NOW() "
                    "WHERE username = :username"
                ),
                {"pw_hash": pw_hash, "username": username.lower()},
            )
            if res.rowcount == 0:
                sys.stderr.write(f"Error: user '{username}' not found.\n")
                sys.exit(1)
        sys.stdout.write(f"Password reset for '{username}'.\n")
    finally:
        await engine.dispose()


def _get_password_from_args_or_prompt(args: argparse.Namespace) -> str:
    """Get password from --password arg or prompt interactively."""
    pw: str | None = getattr(args, "password", None)
    if pw:
        return pw
    import getpass
    pw = getpass.getpass("Password: ")
    if not pw:
        sys.stderr.write("Error: password cannot be empty.\n")
        sys.exit(1)
    if len(pw) < 8:
        sys.stderr.write("Warning: password is shorter than 8 characters.\n")
    return pw


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "migrate":
        database_url = _resolve_database_url(args)
        if getattr(args, "dry_run", False):
            _run_migrate_dry_run()
        else:
            asyncio.run(_run_migrate(database_url))

    elif args.command == "verify":
        database_url = _resolve_database_url(args)
        try:
            session_id = UUID(args.session_id)
        except ValueError:
            sys.stderr.write(
                "Invalid session_id: must be a UUID "
                "(e.g., 550e8400-e29b-41d4-a716-446655440000)\n"
            )
            sys.exit(1)
        exit_code = asyncio.run(_run_verify(database_url, session_id))
        sys.exit(exit_code)

    elif args.command == "tail":
        database_url = _resolve_database_url(args)
        asyncio.run(_run_tail(database_url, args.agent_id, args.kind))

    elif args.command == "budget":
        database_url = _resolve_database_url(args)
        asyncio.run(_run_budget(database_url, args.agent_id))

    elif args.command == "report":
        database_url = _resolve_database_url(args)
        asyncio.run(
            _run_report(
                database_url=database_url,
                report_format=getattr(args, "report_format", "article12"),
                session_id=getattr(args, "session_id", None),
                agent_id=getattr(args, "agent_id", None),
                date_from_str=getattr(args, "date_from", None),
                date_to_str=getattr(args, "date_to", None),
                output_path=getattr(args, "output", None),
            )
        )

    elif args.command == "rotate-chain-key":
        database_url = _resolve_database_url(args)
        if not args.confirm:
            sys.stderr.write(
                "rotate-chain-key: add --confirm to proceed. "
                "Rotation writes a marker row and updates the key registry.\n"
            )
            sys.exit(2)
        asyncio.run(
            _run_rotate_chain_key(
                database_url=database_url,
                new_key_uri=args.new_key_uri,
                operator_id=args.operator_id,
                reason=args.reason,
            )
        )

    elif args.command == "console":
        console_cmd = getattr(args, "console_command", None)
        if console_cmd is None:
            parser.parse_args(["console", "--help"])
            sys.exit(0)
        database_url = _resolve_database_url(args)
        if console_cmd == "add-user":
            password = _get_password_from_args_or_prompt(args)
            asyncio.run(
                _run_console_add_user(database_url, args.username, args.role, password)
            )
        elif console_cmd == "list-users":
            asyncio.run(_run_console_list_users(database_url))
        elif console_cmd == "disable-user":
            asyncio.run(_run_console_disable_user(database_url, args.username))
        elif console_cmd == "reset-password":
            password = _get_password_from_args_or_prompt(args)
            asyncio.run(
                _run_console_reset_password(database_url, args.username, password)
            )
        else:
            parser.parse_args(["console", "--help"])
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)
