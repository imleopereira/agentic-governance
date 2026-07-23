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
    """Apply all DDL files, then run alembic upgrade head.

    Idempotent: DDL files use ``CREATE TABLE IF NOT EXISTS`` and alembic
    tracks applied revisions in ``alembic_version``. Running twice is a
    no-op.

    Why both: the DDL files own the base v0.5 schema (``CREATE TABLE``);
    alembic owns all additive changes after that (v0.6 Ed25519 signing
    columns, v0.6 agent-key tables, rotation markers, revocation chain
    rows, etc.). A fresh install runs DDL + alembic to reach HEAD; an
    upgrade runs alembic only. Shipping DDL without alembic meant every
    fresh v0.6 install ended up on v0.5 schema and silently dropped
    audit rows as ``StoreUnavailableError`` against the missing
    ``signature_status`` column — latent P0, fixed by invoking alembic
    from this entry point.
    """
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

    # Run alembic upgrade head to apply all additive migrations (v0.6
    # signature columns, agent-key tables, rotation markers, etc.).
    # Import lazily — alembic + a sync driver live behind the optional
    # ``[migrations]`` extra to keep asyncpg-only runtime installs slim.
    try:
        from alembic import command as _alembic_command
        from alembic.config import Config as _AlembicConfig
    except ImportError:
        sys.stderr.write(
            "Warning: alembic not installed. Fresh installs require the "
            "[migrations] extra: pip install codeatelier-governance[migrations]. "
            "Audit writes will degrade to StoreUnavailableError against the "
            "v0.5 schema until alembic is run.\n"
        )
        return

    # Resolve ``alembic.ini`` + ``migrations/`` via the package's own
    # resource tree. v0.6.0/0.6.1 resolved this via
    # ``Path(__file__).parent.parent.parent.parent`` — fine for editable
    # checkouts where the parent dirs walk out to the repo root that
    # contains ``alembic.ini``, but broken for every ``pip install``: the
    # walk lands in ``site-packages/`` which has no ``alembic.ini``, the
    # CLI emitted a warning and returned, and fresh installs silently
    # stayed on the v0.5 schema. First audit write then failed with
    # ``StoreUnavailableError`` against the missing ``signature_status``
    # column. v0.6.2 fix: ship ``alembic.ini`` + ``migrations/`` as
    # package data under ``codeatelier_governance/`` and locate them via
    # ``importlib.resources`` so the same code path works for both the
    # wheel-installed case and editable checkouts.
    from importlib import resources

    _alembic_ini_path: Path | None = None
    try:
        _res = resources.files("codeatelier_governance") / "alembic.ini"
        # ``Traversable.is_file()`` returns ``False`` for resources that
        # do exist but live inside a zip — the ``as_file`` context below
        # materialises them to a real path. For the classic file-on-disk
        # wheel install we hit is_file()==True immediately.
        if _res.is_file():
            _alembic_ini_path = Path(str(_res))
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        _alembic_ini_path = None

    if _alembic_ini_path is None or not _alembic_ini_path.is_file():
        sys.stderr.write(
            "Warning: alembic.ini not found inside the codeatelier_governance "
            "package; skipping post-DDL migrations. This should not happen "
            "with a pip-installed wheel — please report as a packaging bug.\n"
        )
        return

    cfg = _AlembicConfig(str(_alembic_ini_path))
    cfg.set_main_option("sqlalchemy.url", _normalize_url_sync(database_url))
    _alembic_command.upgrade(cfg, "head")
    sys.stdout.write("Applied: alembic upgrade head\n")
    sys.stdout.write("Migration complete.\n")


async def _run_verify(database_url: str, session_id: UUID) -> int:
    """Verify the HMAC chain for a session. Returns 0 (clean) or 1 (tampered).

    Checks, in order: the genesis row (the first event's prev_hash must be
    None), each row's HMAC, and the prev_hash -> hmac linkage between
    consecutive rows. This detects in-place tampering, head deletion, interior
    deletion, and reordering. It does NOT detect tail truncation — dropping the
    newest events leaves a self-consistent chain, which requires an external
    high-water-mark — and it verifies under the current GOVERNANCE_AUDIT_SECRET
    only: a chain that spans a key rotation needs the library's rotation-aware
    verifier (``audit.chain.verify_chain_with_rotation``).
    """
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

        records = [
            AuditEventRecord(
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
            for row in rows
        ]

        # Genesis: the session's first event must have no predecessor. A
        # non-None head means the true first event(s) were deleted.
        if records[0].prev_hash is not None:
            sys.stdout.write(
                f"TAMPERED: chain head truncated for session {session_id} "
                f"(first event {records[0].event_id} has a prev_hash; earlier "
                f"events were deleted).\n"
            )
            return 1

        # Per-row HMAC + prev_hash -> hmac linkage. Linkage catches deletion of
        # an interior event that leaves every surviving row individually valid.
        for i, record in enumerate(records):
            if not verify_event(record, secret):
                sys.stdout.write(f"TAMPERED: {record.event_id}\n")
                return 1
            if i > 0 and record.prev_hash != records[i - 1].hmac:
                sys.stdout.write(
                    f"TAMPERED: chain gap before event {record.event_id} "
                    f"(prev_hash does not match the preceding event; an event "
                    f"was deleted).\n"
                )
                return 1

        sys.stdout.write(
            f"OK: {len(records)} events verified for session {session_id}.\n"
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

    from codeatelier_governance.audit.keys import _resolve_uri
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

    # init (single-file recipe scaffolding — F3 v0.7)
    from .init import register_subparser as _register_init
    _register_init(subparsers)

    # recipe (multi-file project scaffolding — v0.7.2)
    from ..recipes import RECIPE_NAMES as _RECIPE_NAMES
    recipe_parser = subparsers.add_parser(
        "recipe",
        help="Scaffold a ready-to-run agent project (e.g. Microsoft AGT).",
    )
    recipe_parser.add_argument(
        "template",
        choices=list(_RECIPE_NAMES),
        help="Which recipe to scaffold.",
    )
    recipe_parser.add_argument(
        "path",
        help="Target directory for the scaffolded project.",
    )
    recipe_parser.add_argument(
        "--force", action="store_true", default=False,
        help="Overwrite the target directory if it already exists.",
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

    elif args.command == "init":
        from .init import run_init
        sys.exit(run_init(args.recipe, force=bool(getattr(args, "force", False))))

    elif args.command == "recipe":
        from .recipe import run_recipe_command
        sys.exit(
            run_recipe_command(
                args.template,
                args.path,
                force=bool(getattr(args, "force", False)),
            )
        )

    else:
        parser.print_help()
        sys.exit(1)
