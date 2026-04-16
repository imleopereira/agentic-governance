#!/bin/bash
# Console Redesign Stage 3: Devil's Advocate + UI/UX review
# Runs after both backend and frontend complete.
# Has the Oversight DA and UI/UX team review the implementation.
# Governed by the SDK.

REPO="/Users/leopereira/code/claude-teams/governance"
LOG="$REPO/scripts/automation/logs/console-review.log"
LOCK="/tmp/ca-console-review.lock"
STAGE="console-redesign-review"

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

# Gate: both backend and frontend must be complete
BACKEND_LOG="$REPO/scripts/automation/logs/console-backend.log"
FRONTEND_LOG="$REPO/scripts/automation/logs/console-frontend.log"

if ! grep -q "END console-redesign-backend (exit=0)" "$BACKEND_LOG" 2>/dev/null; then
    echo "[$(date)] BLOCKED: backend not complete" >> "$LOG"
    exit 1
fi
if ! grep -q "END console-redesign-frontend (exit=0)" "$FRONTEND_LOG" 2>/dev/null; then
    echo "[$(date)] BLOCKED: frontend not complete" >> "$LOG"
    exit 1
fi

# Log to governance SDK
.venv/bin/python -c "
import asyncio, uuid, os
from codeatelier_governance import GovernanceSDK
from codeatelier_governance.audit.models import AuditEvent

async def main():
    async with GovernanceSDK(database_url=os.environ['GOVERNANCE_DATABASE_URL']) as sdk:
        await sdk.presence.heartbeat('console-redesign-review', metadata={
            'stage': 'review', 'purpose': 'da-uiux-approval',
        })
        await sdk.audit.log(AuditEvent(
            agent_id='console-redesign-review',
            kind='review.started',
            session_id=uuid.UUID('d1d2d3d4-e5e6-f7f8-a9a0-b1b2b3b4b5b6'),
            metadata={'stage': 'review', 'reviewers': ['oversight-da','ui-ux-advocate','cybersecurity']},
        ))
asyncio.run(main())
" 2>> "$LOG" || true

PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<'PROMPT_EOF'
You are the Review Orchestrator for the Code Atelier Governance SDK console redesign.

The backend and frontend implementation stages are complete. Your job is to run the Devils Advocate and UI/UX team reviews on the ACTUAL implementation, not the spec.

DO ALL OF THESE:

1. Read the implementation: examine every changed file in src/codeatelier_governance/console/ and console/src/. Compare against the approved spec at .agent-outputs/ui-ux/console-redesign-spec.md.

2. Run the test suite: pytest tests/ -q for unit tests. cd console and npm run build for frontend. mypy --strict src/codeatelier_governance/console/ for type check.

3. Devils Advocate Review be ruthless: Does EVERY DX must-fix from .agent-outputs/dx-advocate/console-redesign-review.md exist in the code? Empty states implemented for all 3 views? SSE disconnect visible banner? Keyboard conflicts K-for-kill removed and Ctrl+A fixed? Self-approval prevention server-enforced AND fail-closed? Kill switch requires reason field and is audit-logged? No pg_cron anywhere? Alembic migrations not raw ALTER TABLE? NOTIFY payload size guard? Batch approve cap of 50?

4. UI/UX Review: Do wireframes match implementation? All color tokens from spec used correctly? WCAG AA contrast ratios met? All ARIA roles from spec sections 2.7 3.7 4.7 present? Dark-mode-first? Responsive breakpoints implemented?

5. Security Spot Check: SSE auth re-validation interval? Rationale rendered as text not HTML? No audit secret in SSE payloads? Grant/deny enforce claim holder match?

Write 3 review files: .agent-outputs/oversight/console-implementation-review.md with DA verdict APPROVE or REJECT plus specific issues. .agent-outputs/ui-ux/console-implementation-review.md with UI/UX verdict APPROVE or NEEDS-WORK plus specific issues. .agent-outputs/cybersecurity/console-implementation-spot-check.md with Security spot-check PASS or FAIL plus findings.

If ANY review is REJECT or FAIL list the specific files and line numbers that need fixing. If ALL reviews APPROVE write a summary to .agent-outputs/oversight/console-redesign-approved.md with the text APPROVED FOR MERGE at the top.
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
            agent_id='console-redesign-review',
            kind='review.completed',
            session_id=uuid.UUID('d1d2d3d4-e5e6-f7f8-a9a0-b1b2b3b4b5b6'),
            metadata={'stage': 'review', 'exit_code': int(sys.argv[1])},
        ))
        await sdk.presence.mark_idle('console-redesign-review')
asyncio.run(main())
" "$EXIT_CODE" 2>> "$LOG" || true

echo "[$(date)] END $STAGE (exit=$EXIT_CODE)" >> "$LOG"
exit $EXIT_CODE
