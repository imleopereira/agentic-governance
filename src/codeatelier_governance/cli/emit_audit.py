#!/usr/bin/env python3
"""CLI tool for emitting audit events from Claude Code agent sessions.

Designed for the apex content pipeline where agents run inside Claude Code
and can only call external tools via Bash. Each invocation instantiates the
SDK, emits one event, prints the event_id to stdout, and exits.

Usage (from a Claude agent's Bash tool call):

    EVENT_ID=$(python3 governance/src/codeatelier_governance/cli/emit_audit.py \
        --kind "content.draft_complete" \
        --agent-id "article-writer" \
        --session-id "$APEX_AUDIT_SESSION" \
        --metadata '{"slug":"agentic-risk-standard","content_length_chars":28033}')

    # Chain events via parent-event-id:
    EVENT_ID=$(python3 governance/src/codeatelier_governance/cli/emit_audit.py \
        --kind "content.da_verdict" \
        --agent-id "devils-advocate" \
        --session-id "$APEX_AUDIT_SESSION" \
        --parent-event-id "$EVENT_ID" \
        --metadata '{"slug":"agentic-risk-standard","verdict":"SHIP"}')

Feature flag: APEX_CONTENT_AUDIT_ENABLED=true (default: disabled).
When disabled, prints a null UUID and exits 0.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from uuid import UUID


NULL_UUID = "00000000-0000-0000-0000-000000000000"


def main() -> None:
    if os.environ.get("APEX_CONTENT_AUDIT_ENABLED", "").lower() != "true":
        print(NULL_UUID)
        return

    parser = argparse.ArgumentParser(description="Emit a governance audit event")
    parser.add_argument("--kind", required=True, help="Event kind (e.g. content.draft_complete)")
    parser.add_argument("--agent-id", required=True, help="Agent identifier")
    parser.add_argument("--session-id", required=True, help="Session UUID for this article lifecycle")
    parser.add_argument("--parent-event-id", default=None, help="Parent event UUID for chain linking")
    parser.add_argument("--metadata", default="{}", help="JSON string of event metadata")
    args = parser.parse_args()

    if len(args.metadata) > 65536:
        print("Metadata exceeds 64KB limit", file=sys.stderr)
        print(NULL_UUID)
        sys.exit(1)

    try:
        metadata = json.loads(args.metadata)
    except json.JSONDecodeError as e:
        print(f"Invalid metadata JSON: {e}", file=sys.stderr)
        print(NULL_UUID)
        sys.exit(1)

    try:
        from codeatelier_governance import GovernanceSDK
        from codeatelier_governance.audit.models import AuditEvent

        db_url = os.environ.get("GOVERNANCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not db_url:
            print("No DATABASE_URL set, skipping audit", file=sys.stderr)
            print(NULL_UUID)
            return

        async def _emit() -> str:
            async with GovernanceSDK(database_url=db_url) as sdk:
                event = AuditEvent(
                    session_id=UUID(args.session_id),
                    agent_id=args.agent_id,
                    parent_event_id=UUID(args.parent_event_id) if args.parent_event_id and args.parent_event_id != NULL_UUID else None,
                    kind=args.kind,
                    metadata=metadata,
                )
                record = await sdk.audit.log(event)
                return str(record.event_id)

        event_id = asyncio.run(_emit())
        print(event_id)

    except Exception as e:
        print(f"Audit emit failed: {e}", file=sys.stderr)
        print(NULL_UUID)


if __name__ == "__main__":
    main()
