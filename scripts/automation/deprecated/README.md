# Deprecated automation scripts

These scripts were part of the console redesign 3-stage pipeline that ran
during v0.4 / v0.5 development. They are retained for historical reference
only and MUST NOT be invoked in v0.6 or later.

**Superseded by**: v0.6 console honesty pass (F1 in the v0.6 major-release PRD).
The F1 work replaces the multi-stage "redesign" flow with in-place, verifiable
console edits — no separate pipeline, no drift between stages.

## Scripts in this directory

- `console-redesign-backend.sh`
- `console-redesign-frontend.sh`
- `console-redesign-pipeline.sh`
- `console-redesign-review.sh`

## Why keep them at all?

Running them is a footgun (they assume v0.4 file layout and will overwrite
F1 work), but deleting them severs the git history for anyone bisecting
console regressions. The compromise: move them out of the active
`scripts/automation/` path so the cron installer and developer muscle
memory no longer reach them, while leaving the files in the tree.

Do not add new files here. If you need a new automation script, add it to
`scripts/automation/` directly.
