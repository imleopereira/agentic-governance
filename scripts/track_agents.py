#!/usr/bin/env python3
"""Track all agents in a Code Atelier Governance deployment.

Queries the governance DB to discover agents, display their status,
policies, usage, and recent violations. Standalone script -- not part
of the SDK package.

Usage:
    python scripts/track_agents.py --database-url postgresql://...
    GOVERNANCE_DATABASE_URL=postgresql://... python scripts/track_agents.py
    python scripts/track_agents.py --json          # machine-readable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any


def _resolve_database_url(args: argparse.Namespace) -> str:
    """Resolve DB URL from args or env var."""
    url: str | None = getattr(args, "database_url", None)
    if not url:
        url = os.environ.get("GOVERNANCE_DATABASE_URL")
    if not url:
        sys.stderr.write(
            "Error: --database-url is required (or set GOVERNANCE_DATABASE_URL).\n"
        )
        sys.exit(2)
    return url


def _normalize_url(url: str) -> str:
    """Convert postgresql:// to postgresql+asyncpg:// for SQLAlchemy async."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


async def _fetch_agent_data(database_url: str) -> list[dict[str, Any]]:
    """Query governance tables and build per-agent summaries."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_normalize_url(database_url))
    try:
        async with engine.connect() as conn:
            # 1. Discover agents + basic audit stats
            result = await conn.execute(
                text(
                    "SELECT agent_id, "
                    "       COUNT(*) AS event_count, "
                    "       MAX(created_at) AS last_active "
                    "FROM governance_audit_events "
                    "GROUP BY agent_id "
                    "ORDER BY last_active DESC"
                )
            )
            audit_rows = {r["agent_id"]: dict(r._mapping) for r in result}

            # 2. Discover agents from policies (may have policies but no events)
            result = await conn.execute(
                text(
                    "SELECT agent_id, policy_type, policy_json, updated_at "
                    "FROM governance_policies "
                    "ORDER BY agent_id"
                )
            )
            policy_rows: dict[str, dict[str, Any]] = {}
            for r in result:
                aid = r._mapping["agent_id"]
                if aid not in policy_rows:
                    policy_rows[aid] = {}
                policy_rows[aid][r._mapping["policy_type"]] = {
                    "policy": r._mapping["policy_json"],
                    "updated_at": r._mapping["updated_at"],
                }

            # 3. Daily cost usage (today)
            today = datetime.now(timezone.utc).date()
            result = await conn.execute(
                text(
                    "SELECT agent_id, usd_used, tokens_used "
                    "FROM governance_cost_agent_daily "
                    "WHERE day_utc = :today"
                ),
                {"today": today},
            )
            daily_usage = {
                r._mapping["agent_id"]: {
                    "usd_today": float(r._mapping["usd_used"]),
                    "tokens_today": int(r._mapping["tokens_used"]),
                }
                for r in result
            }

            # 4. Active sessions per agent
            result = await conn.execute(
                text(
                    "SELECT agent_id, "
                    "       COUNT(*) AS session_count, "
                    "       SUM(usd_used) AS total_usd, "
                    "       SUM(tokens_used) AS total_tokens "
                    "FROM governance_cost_session_usage "
                    "GROUP BY agent_id"
                )
            )
            session_usage = {
                r._mapping["agent_id"]: {
                    "session_count": int(r._mapping["session_count"]),
                    "total_usd": float(r._mapping["total_usd"] or 0),
                    "total_tokens": int(r._mapping["total_tokens"] or 0),
                }
                for r in result
            }

            # 5. Recent violations (scope.denied and budget.exceeded events)
            result = await conn.execute(
                text(
                    "SELECT agent_id, kind, created_at, metadata_json "
                    "FROM governance_audit_events "
                    "WHERE kind IN ('scope.denied', 'budget.exceeded', "
                    "               'scope.violation', 'budget.limit_hit') "
                    "ORDER BY created_at DESC "
                    "LIMIT 100"
                )
            )
            violations: dict[str, list[dict[str, Any]]] = {}
            for r in result:
                aid = r._mapping["agent_id"]
                violations.setdefault(aid, []).append({
                    "kind": r._mapping["kind"],
                    "created_at": r._mapping["created_at"],
                    "metadata": r._mapping["metadata_json"],
                })

        # Merge all agent IDs
        all_agents = sorted(
            set(audit_rows) | set(policy_rows) | set(daily_usage) | set(session_usage)
        )

        agents: list[dict[str, Any]] = []
        for aid in all_agents:
            audit = audit_rows.get(aid, {})
            policies = policy_rows.get(aid, {})
            daily = daily_usage.get(aid, {})
            sessions = session_usage.get(aid, {})
            viols = violations.get(aid, [])

            scope_policy = policies.get("scope")
            budget_policy = policies.get("budget")

            agent_info: dict[str, Any] = {
                "agent_id": aid,
                "last_active": audit.get("last_active"),
                "event_count": audit.get("event_count", 0),
                "scope_policy": _format_scope(scope_policy) if scope_policy else None,
                "budget_policy": (
                    _format_budget(budget_policy) if budget_policy else None
                ),
                "usage": {
                    "usd_today": daily.get("usd_today", 0.0),
                    "tokens_today": daily.get("tokens_today", 0),
                    "session_count": sessions.get("session_count", 0),
                    "total_session_usd": sessions.get("total_usd", 0.0),
                    "total_session_tokens": sessions.get("total_tokens", 0),
                },
                "recent_violations": viols[:5],
            }
            agents.append(agent_info)

        return agents
    finally:
        await engine.dispose()


def _format_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """Extract readable scope policy info."""
    p = scope["policy"]
    return {
        "allowed_tools": p.get("allowed_tools", []),
        "updated_at": scope["updated_at"],
    }


def _format_budget(budget: dict[str, Any]) -> dict[str, Any]:
    """Extract readable budget policy info."""
    p = budget["policy"]
    return {
        "per_session_usd": p.get("per_session_usd"),
        "per_session_tokens": p.get("per_session_tokens"),
        "per_agent_usd_daily": p.get("per_agent_usd_daily"),
        "per_session_seconds": p.get("per_session_seconds"),
        "updated_at": budget["updated_at"],
    }


def _print_table(agents: list[dict[str, Any]]) -> None:
    """Print agents as a formatted table to stdout."""
    if not agents:
        sys.stdout.write("No agents found in governance database.\n")
        return

    # Header
    sys.stdout.write(
        f"{'AGENT ID':<30s}  {'LAST ACTIVE':<20s}  {'EVENTS':>7s}  "
        f"{'SCOPE':>8s}  {'BUDGET':>8s}  "
        f"{'USD TODAY':>10s}  {'TOKENS TODAY':>13s}  {'VIOLATIONS':>10s}\n"
    )
    sys.stdout.write("-" * 120 + "\n")

    for a in agents:
        last_active = ""
        if a["last_active"]:
            ts = a["last_active"]
            if isinstance(ts, datetime):
                last_active = ts.strftime("%Y-%m-%d %H:%M")
            else:
                last_active = str(ts)[:16]

        scope_status = "yes" if a["scope_policy"] else "-"
        budget_status = "yes" if a["budget_policy"] else "-"
        violations = len(a["recent_violations"])

        sys.stdout.write(
            f"{a['agent_id']:<30s}  {last_active:<20s}  "
            f"{a['event_count']:>7d}  {scope_status:>8s}  {budget_status:>8s}  "
            f"${a['usage']['usd_today']:>9.4f}  "
            f"{a['usage']['tokens_today']:>13,d}  "
            f"{violations:>10d}\n"
        )

    sys.stdout.write("-" * 120 + "\n")
    sys.stdout.write(f"Total agents: {len(agents)}\n\n")

    # Detail section for agents with policies or violations
    for a in agents:
        has_detail = a["scope_policy"] or a["budget_policy"] or a["recent_violations"]
        if not has_detail:
            continue

        sys.stdout.write(f"--- {a['agent_id']} ---\n")

        if a["scope_policy"]:
            tools = a["scope_policy"]["allowed_tools"]
            sys.stdout.write(f"  Scope: {len(tools)} tools allowed")
            if tools:
                preview = ", ".join(sorted(tools)[:5])
                if len(tools) > 5:
                    preview += f" (+{len(tools) - 5} more)"
                sys.stdout.write(f": {preview}")
            sys.stdout.write("\n")

        if a["budget_policy"]:
            bp = a["budget_policy"]
            parts = []
            if bp.get("per_session_usd"):
                parts.append(f"${bp['per_session_usd']}/session")
            if bp.get("per_agent_usd_daily"):
                parts.append(f"${bp['per_agent_usd_daily']}/day")
            if bp.get("per_session_tokens"):
                parts.append(f"{bp['per_session_tokens']:,} tokens/session")
            if bp.get("per_session_seconds"):
                parts.append(f"{bp['per_session_seconds']}s/session")
            sys.stdout.write(f"  Budget: {', '.join(parts) or 'configured'}\n")
            sys.stdout.write(
                f"  Sessions: {a['usage']['session_count']}, "
                f"total ${a['usage']['total_session_usd']:.4f}, "
                f"{a['usage']['total_session_tokens']:,} tokens\n"
            )

        if a["recent_violations"]:
            sys.stdout.write(f"  Recent violations ({len(a['recent_violations'])}):\n")
            for v in a["recent_violations"]:
                ts = v["created_at"]
                if isinstance(ts, datetime):
                    ts = ts.strftime("%Y-%m-%d %H:%M")
                sys.stdout.write(f"    [{ts}] {v['kind']}\n")

        sys.stdout.write("\n")


def _print_json(agents: list[dict[str, Any]]) -> None:
    """Print agents as JSON to stdout."""

    def _default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return str(obj)

    sys.stdout.write(json.dumps(agents, indent=2, default=_default) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="track_agents",
        description="Track all agents in a Code Atelier Governance deployment.",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="PostgreSQL connection string (or set GOVERNANCE_DATABASE_URL)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output as JSON instead of table",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    database_url = _resolve_database_url(args)
    agents = asyncio.run(_fetch_agent_data(database_url))

    if args.json:
        _print_json(agents)
    else:
        _print_table(agents)


if __name__ == "__main__":
    main()
