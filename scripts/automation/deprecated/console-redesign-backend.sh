#!/bin/bash
# Console Redesign Stage 1: Backend implementation
# Implements SSE infrastructure, new endpoints, Postgres triggers, Alembic migrations.
# Governed by the SDK. Runs in parallel with frontend stages.

REPO="/Users/leopereira/code/claude-teams/governance"
LOG="$REPO/scripts/automation/logs/console-backend.log"
LOCK="/tmp/ca-console-backend.lock"
STAGE="console-redesign-backend"

if [ -e "$LOCK" ]; then
    echo "[$(date)] SKIP: lock file exists" >> "$LOG"
    exit 0
fi
touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT

export PATH="/Users/leopereira/.local/bin:/opt/homebrew/bin:/opt/homebrew/opt/node@22/bin:/Users/leopereira/.pyenv/shims:$PATH"
export CA_RUN_CONTEXT=$STAGE
export GOV_SDK_ENABLED=true
export GOVERNANCE_DATABASE_URL="${GOVERNANCE_DATABASE_URL:-postgresql://governance:governance@localhost:5435/governance_qa}"
export GOVERNANCE_AUDIT_SECRET="${GOVERNANCE_AUDIT_SECRET:-$(cat $HOME/.governance-audit-secret 2>/dev/null || echo '')}"

[ -f "$HOME/.claude-cron-auth" ] && source "$HOME/.claude-cron-auth"

cd "$REPO" || {
    echo "[$(date)] FATAL: could not cd into $REPO" >> "$LOG"
    exit 1
}

echo "[$(date)] START $STAGE" >> "$LOG"

# Gate: spec must exist and be team-approved
SPEC="$REPO/.agent-outputs/ui-ux/console-redesign-spec.md"
if [ ! -f "$SPEC" ]; then
    echo "[$(date)] BLOCKED: redesign spec not found" >> "$LOG"
    exit 1
fi

# Log to governance SDK
.venv/bin/python -c "
import asyncio, uuid, os
from codeatelier_governance import GovernanceSDK
from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.scope.models import ScopePolicy

async def main():
    async with GovernanceSDK(database_url=os.environ['GOVERNANCE_DATABASE_URL']) as sdk:
        sdk.scope.register(ScopePolicy(
            agent_id='console-redesign-backend',
            allowed_tools=frozenset({'write_code','run_tests','create_migration','read_file'}),
        ))
        await sdk.presence.heartbeat('console-redesign-backend', metadata={
            'stage': 'backend', 'purpose': 'console-redesign',
        })
        await sdk.audit.log(AuditEvent(
            agent_id='console-redesign-backend',
            kind='implementation.started',
            session_id=uuid.UUID('b1b2b3b4-c5c6-d7d8-e9e0-f1f2f3f4f5f6'),
            metadata={'stage': 'backend', 'spec': '$SPEC'},
        ))
asyncio.run(main())
" 2>> "$LOG" || true

PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<'PROMPT_EOF'
You are the Backend Implementation agent for the Code Atelier Governance SDK console redesign.

READ THESE FILES FIRST:
- CLAUDE.md
- .agent-outputs/ui-ux/console-redesign-spec.md
- .agent-outputs/cto/console-redesign-review.md
- .agent-outputs/cybersecurity/console-redesign-review.md
- src/codeatelier_governance/console/app.py
- src/codeatelier_governance/console/ddl.sql

YOUR TASK: Implement the v0.5 approved backend scope. Follow CTO and Security conditions exactly.

IMPLEMENT IN ORDER:

1. Alembic migration for 3 new columns on governance_gates_pending: reviewer_id UUID nullable, reviewing_since TIMESTAMPTZ nullable, rationale VARCHAR 2000 nullable.

2. Postgres LISTEN/NOTIFY triggers in a new DDL file: notify_governance_event on governance_audit_events INSERT, notify_gate_change on governance_gates_pending INSERT/UPDATE, notify_presence_change on governance_agent_presence INSERT/UPDATE. Add 8KB payload size guard.

3. SSE endpoint GET /api/stream/events: one shared asyncpg LISTEN connection per worker, fan-out via asyncio.Queue per client, session cookie auth with re-validation every 30s, backpressure drop oldest if queue over 200, heartbeat keepalive every 15s, Last-Event-ID replay with cap of 500 events, reconnect-friendly.

4. New endpoints with session cookie auth: POST /api/agents/AGENTID/kill admin-only audit-logged requires reason field, GET /api/events/stats event counts last hour per kind events/min, GET /api/gates/GATEID/context rich context for approval, POST /api/gates/GATEID/claim reviewer presence WHERE reviewer_id IS NULL, POST /api/gates/GATEID/escalate.

5. Enhanced endpoints: POST /api/gates/GATEID/deny add rationale field store in audit event, GET /api/gates/pending include reviewer_id reviewing_since, POST /api/gates/GATEID/grant enforce acting user matches claim holder.

6. Self-approval prevention server-enforced: in grant and deny compare session user_id vs agent operator_id. Fail-closed if operator_id is missing BLOCK the action. Return 403 with clear message.

7. Batch approve POST /api/gates/batch-approve: hard cap of 50, server-side re-verify each is LOW risk, individual audit events per approval, admin-only.

CONSTRAINTS: No pg_cron. Use SSE heartbeat loop for stale claim cleanup. No new infrastructure. Just Postgres. All new endpoints follow existing FastAPI patterns in app.py. Every endpoint must have proper error handling. Run tests after: pytest tests/ -q. Run type check: mypy --strict src/codeatelier_governance/console/. DO NOT commit.
PROMPT_EOF

claude --print -p "$(cat "$PROMPT_FILE")" >> "$LOG" 2>&1
rm -f "$PROMPT_FILE"
EXIT_CODE=$?

# Log completion to governance SDK
.venv/bin/python -c "
import asyncio, uuid, os, sys
from codeatelier_governance import GovernanceSDK
from codeatelier_governance.audit.models import AuditEvent

async def main():
    async with GovernanceSDK(database_url=os.environ['GOVERNANCE_DATABASE_URL']) as sdk:
        await sdk.audit.log(AuditEvent(
            agent_id='console-redesign-backend',
            kind='implementation.completed',
            session_id=uuid.UUID('b1b2b3b4-c5c6-d7d8-e9e0-f1f2f3f4f5f6'),
            metadata={'stage': 'backend', 'exit_code': int(sys.argv[1])},
        ))
        await sdk.presence.mark_idle('console-redesign-backend')
asyncio.run(main())
" "$EXIT_CODE" 2>> "$LOG" || true

echo "[$(date)] END $STAGE (exit=$EXIT_CODE)" >> "$LOG"
exit $EXIT_CODE
