"""Dogfooding script: emit audit events for v0.2.2 implementation items.

Usage:
    GOVERNANCE_AUDIT_SECRET="qa-harness-deadbeef-cafe-1234-5678-9abc-def0-1234-5678-9abcdef01234" \
    GOVERNANCE_DATABASE_URL="postgresql://user:pass@host/db" \
    .venv/bin/python scripts/dogfood_v022.py
"""
from __future__ import annotations

import asyncio
import os
import sys

AGENT_ID = "v022-engineer"


async def main() -> None:
    db_url = os.environ.get(
        "GOVERNANCE_DATABASE_URL",
        "",
    )
    if "GOVERNANCE_AUDIT_SECRET" not in os.environ:
        os.environ["GOVERNANCE_AUDIT_SECRET"] = (
            "qa-harness-deadbeef-cafe-1234-5678-9abc-def0-1234-5678-9abcdef01234"
        )

    from codeatelier_governance import GovernanceSDK
    from codeatelier_governance.audit.models import AuditEvent

    async with GovernanceSDK(database_url=db_url) as sdk:
        items = [
            {
                "kind": "v022.item.complete",
                "metadata": {
                    "item": "G15",
                    "title": "Time limits per session",
                    "status": "complete",
                    "changes": [
                        "per_session_seconds field on BudgetPolicy",
                        "session start time tracking in stores",
                        "time limit check in CostModule.check_or_raise()",
                        "started_at column in DDL",
                        "5 new tests",
                    ],
                },
            },
            {
                "kind": "v022.item.complete",
                "metadata": {
                    "item": "G3",
                    "title": "Built-in model pricing",
                    "status": "complete",
                    "changes": [
                        "cost/pricing.py with MODEL_PRICING (20+ models)",
                        "estimate_cost() with prefix matching",
                        "CostModule.track_usage() convenience method",
                        "OpenAI wrapper uses centralized pricing",
                        "9 new tests",
                    ],
                },
            },
            {
                "kind": "v022.item.complete",
                "metadata": {
                    "item": "G10",
                    "title": "Hidden tool policies",
                    "status": "complete",
                    "changes": [
                        "hidden_tools field on ScopePolicy",
                        "ScopeModule.filter_tools() method",
                        "LangChain handler integration",
                        "5 new tests",
                    ],
                },
            },
            {
                "kind": "v022.item.complete",
                "metadata": {
                    "item": "AUTH-1",
                    "title": "Console auth model",
                    "status": "complete",
                    "changes": [
                        "PBKDF2 password hashing (stdlib, no new deps)",
                        "console/ddl.sql: users + sessions tables",
                        "Three-mode auth middleware",
                        "Login/logout/me + admin CRUD endpoints",
                        "CLI user management commands",
                        "14 new tests",
                    ],
                },
            },
        ]

        for item in items:
            event = AuditEvent(
                agent_id=AGENT_ID,
                kind=item["kind"],
                metadata=item["metadata"],
            )
            record = await sdk.audit.log(event)
            sys.stdout.write(
                f"  logged: {item['metadata']['item']} -> "
                f"event_id={record.event_id}\n"
            )

        sys.stdout.write(
            f"\n  All {len(items)} v0.2.2 audit events logged "
            f"for agent_id={AGENT_ID}\n"
        )


if __name__ == "__main__":
    asyncio.run(main())
