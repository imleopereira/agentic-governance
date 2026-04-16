#!/bin/bash
# Console Redesign Pipeline Orchestrator
# Runs the full implementation pipeline:
#   Stage 1: Backend (SSE, endpoints, migrations) — launches immediately
#   Stage 2: Frontend (3 views) — waits for backend
#   Stage 3: DA + UI/UX review — waits for both
#
# All stages are governed by the SDK (audit, scope, presence).

REPO="/Users/leopereira/code/claude-teams/governance"
LOG="$REPO/scripts/automation/logs/console-pipeline.log"
LOCK="/tmp/ca-console-pipeline.lock"

if [ -e "$LOCK" ]; then
    echo "[$(date)] SKIP: pipeline lock exists" >> "$LOG"
    exit 0
fi
touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT

export PATH="/Users/leopereira/.local/bin:/opt/homebrew/bin:/opt/homebrew/opt/node@22/bin:/Users/leopereira/.pyenv/shims:$PATH"
export GOVERNANCE_DATABASE_URL="${GOVERNANCE_DATABASE_URL:-postgresql://governance:governance@localhost:5435/governance_qa}"
export GOVERNANCE_AUDIT_SECRET="${GOVERNANCE_AUDIT_SECRET:-$(cat $HOME/.governance-audit-secret 2>/dev/null || echo '')}"

[ -f "$HOME/.claude-cron-auth" ] && source "$HOME/.claude-cron-auth"

echo "" >> "$LOG"
echo "============================================================" >> "$LOG"
echo "[$(date)] PIPELINE START: Console Redesign v0.5" >> "$LOG"
echo "============================================================" >> "$LOG"

# --- Stage 1: Backend (runs immediately) ---
echo "[$(date)] Launching Stage 1: Backend..." >> "$LOG"
bash "$REPO/scripts/automation/console-redesign-backend.sh" >> "$LOG" 2>&1
BACKEND_EXIT=$?
echo "[$(date)] Stage 1 Backend exit=$BACKEND_EXIT" >> "$LOG"

if [ $BACKEND_EXIT -ne 0 ]; then
    echo "[$(date)] PIPELINE ABORTED: Backend failed" >> "$LOG"
    exit 1
fi

# --- Stage 2: Frontend (depends on backend) ---
echo "[$(date)] Launching Stage 2: Frontend..." >> "$LOG"
bash "$REPO/scripts/automation/console-redesign-frontend.sh" >> "$LOG" 2>&1
FRONTEND_EXIT=$?
echo "[$(date)] Stage 2 Frontend exit=$FRONTEND_EXIT" >> "$LOG"

if [ $FRONTEND_EXIT -ne 0 ]; then
    echo "[$(date)] PIPELINE ABORTED: Frontend failed" >> "$LOG"
    exit 1
fi

# --- Stage 3: DA + UI/UX Review ---
echo "[$(date)] Launching Stage 3: Review..." >> "$LOG"
bash "$REPO/scripts/automation/console-redesign-review.sh" >> "$LOG" 2>&1
REVIEW_EXIT=$?
echo "[$(date)] Stage 3 Review exit=$REVIEW_EXIT" >> "$LOG"

# --- Final status ---
echo "" >> "$LOG"
echo "============================================================" >> "$LOG"
if [ $REVIEW_EXIT -eq 0 ] && [ -f "$REPO/.agent-outputs/oversight/console-redesign-approved.md" ]; then
    echo "[$(date)] PIPELINE COMPLETE: All stages passed, DA approved" >> "$LOG"
else
    echo "[$(date)] PIPELINE COMPLETE: Review stage exit=$REVIEW_EXIT — check review files" >> "$LOG"
fi
echo "============================================================" >> "$LOG"

exit $REVIEW_EXIT
