#!/bin/bash
# Console Redesign Stage 2: Frontend implementation (3 views in parallel)
# Implements Topology Grid, Live Event Stream, HITL Approval Queue.
# Governed by the SDK. Depends on backend stage completing.

REPO="/Users/leopereira/code/claude-teams/governance"
LOG="$REPO/scripts/automation/logs/console-frontend.log"
LOCK="/tmp/ca-console-frontend.lock"
STAGE="console-redesign-frontend"

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

# Gate: backend log must show completion
BACKEND_LOG="$REPO/scripts/automation/logs/console-backend.log"
if ! grep -q "END console-redesign-backend (exit=0)" "$BACKEND_LOG" 2>/dev/null; then
    echo "[$(date)] BLOCKED: backend stage not complete or failed" >> "$LOG"
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
            agent_id='console-redesign-frontend',
            allowed_tools=frozenset({'write_code','run_tests','read_file','write_component'}),
        ))
        await sdk.presence.heartbeat('console-redesign-frontend', metadata={
            'stage': 'frontend', 'purpose': 'console-redesign',
        })
        await sdk.audit.log(AuditEvent(
            agent_id='console-redesign-frontend',
            kind='implementation.started',
            session_id=uuid.UUID('c1c2c3c4-d5d6-e7e8-f9f0-a1a2a3a4a5a6'),
            metadata={'stage': 'frontend', 'views': ['topology','event-stream','hitl-queue']},
        ))
asyncio.run(main())
" 2>> "$LOG" || true

PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<'PROMPT_EOF'
You are the Frontend Implementation agent for the Code Atelier Governance SDK console redesign.

READ THESE FILES FIRST: .agent-outputs/ui-ux/console-redesign-spec.md sections 2-4, .agent-outputs/dx-advocate/console-redesign-review.md, .agent-outputs/cpo/console-redesign-review.md, console/src/ existing Next.js frontend, console/package.json.

YOUR TASK: Implement the v0.5 approved frontend scope. 3 views.

APPROVED SCOPE: Topology Grid view ONLY no graph. Live Event Stream with chain verify button. HITL Queue core approve/deny with rationale no batch no reviewer presence no escalation timers. Sidebar navigation replace top bar. SSE client hook useEventStream.

DX MUST-FIX non-negotiable: 1 Empty states for ALL views. 2 Waiting for events skeleton state on first load. 3 SSE disconnect banner with reconnect countdown. 4 Remove K for kill conflicts with J/K nav use Cmd+K palette instead. 5 Remove Ctrl+A for batch select conflicts with OS select-all. 6 Landing page equals Topology view.

IMPLEMENT IN ORDER:

1. Shared infrastructure: Zustand store for real-time state, useEventStream hook SSE client with auto-reconnect Last-Event-ID disconnect banner, Sidebar layout component 260px collapsible to 48px icons, Color tokens in CSS custom properties dark-mode-first, Virtual scroll component for event list max 200 DOM nodes.

2. Agent Topology grid view: KPI strip live/idle/warn/dead counts events/min, Agent cards with status badges color plus icon never color alone, Budget micro-bars per agent, Detail panel expand on click metrics policies kill button admin only, Empty state No agents registered, Status transitions smooth CSS animations 300ms.

3. Live Event Stream: SSE-driven real-time list with virtual scrolling, Filter bar agent kind session severity, Smart batching over 10 events in 200ms equals collapsed summary row, Auto-scroll with smart pause user scrolls down equals pause N new events pill, Inline event detail expansion, Verify Chain button calls /api/session/ID/verify, Empty state Waiting for events.

4. HITL Approval Queue: Urgency-sorted cards URGENT/NORMAL/LOW, Approve button with confirmation dialog, Deny button two-click confirm rationale required stored in audit trail, Resolution history table below, Tab title N Approval Queue with pending count, Empty state No pending approvals.

5. Accessibility: All ARIA roles as specified in spec, Focus rings on all interactive elements, aria-live regions for real-time updates, Keyboard navigation J/K for lists Enter to expand Esc to close, Screen reader labels on all badges and buttons.

CONSTRAINTS: Dark-mode-first. Under 100ms interaction budget. Use existing TanStack Query for REST calls Zustand for SSE state. Existing packages only. Mobile responsive but defer PWA. Run after cd console and npm run build must compile clean. DO NOT commit.
PROMPT_EOF

claude --print -p "$(cat "$PROMPT_FILE")" >> "$LOG" 2>&1
rm -f "$PROMPT_FILE"
EXIT_CODE=$?

# Log completion
.venv/bin/python -c "
import asyncio, uuid, os, sys
from codeatelier_governance import GovernanceSDK
from codeatelier_governance.audit.models import AuditEvent

async def main():
    async with GovernanceSDK(database_url=os.environ['GOVERNANCE_DATABASE_URL']) as sdk:
        await sdk.audit.log(AuditEvent(
            agent_id='console-redesign-frontend',
            kind='implementation.completed',
            session_id=uuid.UUID('c1c2c3c4-d5d6-e7e8-f9f0-a1a2a3a4a5a6'),
            metadata={'stage': 'frontend', 'exit_code': int(sys.argv[1])},
        ))
        await sdk.presence.mark_idle('console-redesign-frontend')
asyncio.run(main())
" "$EXIT_CODE" 2>> "$LOG" || true

echo "[$(date)] END $STAGE (exit=$EXIT_CODE)" >> "$LOG"
exit $EXIT_CODE
