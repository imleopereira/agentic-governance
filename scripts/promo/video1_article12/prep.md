# Video 1 — Pre-Recording Checklist

**Do not hit record until every box below is checked.** Work through the list
in order; some items build on earlier ones (e.g. fresh DB before seed).

---

## A. DA unblock gates (must all be green before recording day)

These are program-level gates from the earlier DA verdict. If any is red, the
recording is postponed.

- [ ] **A1. v0.6.2 is live on PyPI.**
  - Verify: `curl -sI https://pypi.org/pypi/code-atelier-governance/0.6.2/json | head -1` returns `200`.
  - Why: v0.6.0 and v0.6.1 wheels are broken; recording against anything else burns the install demo.
- [ ] **A2. `qa/test_clean_install.sh` is green against the published 0.6.2 wheel.**
  - Verify: clean venv, `pip install "code-atelier-governance[migrations]==0.6.2"`, run the QA script, exit 0.
  - Why: proves the demo install line actually works on a buyer's laptop, not just ours.
- [ ] **A3. `scripts/promo/video1_article12/seed.py` exists and runs green.**
  - Verify: seed script (authored in parallel by the Seed Builder agent) runs end-to-end against a fresh DB and produces: 4 agents, 1 halted, 1 key rotation mid-history, ~245 events, no legacy pills.
  - Why: voiceover Beat 2 hard-codes these numbers. If seed drifts, the script is wrong on camera.
- [ ] **A4. Rollback narrative reconciled (v0.6.1 NOT on PyPI).**
  - Verify: `curl -sI https://pypi.org/pypi/code-atelier-governance/0.6.1/json | head -1` returns `404`.
  - Why: we can't sell "audit-ready today" while a broken wheel is still installable by default resolvers.
- [ ] **A5. Keystore-fingerprint shot confirmed cut from Video 1 (inherits to Video 3 note only).**
  - Verify: read `shots.md` — the "Note for Video 3 only" block is present, and no beat in `voiceover.md` references a fingerprint reveal.
  - Why: the drawer hash doesn't match a base64 PEM; showing it misleads a security buyer. Keeping the note prevents the trap resurfacing for Video 3.

---

## B. DA's 9 gotchas (recording-day, in order)

### 1. Fresh demo DB (dropped + recreated)

- [ ] **Do:** drop the demo Postgres DB, recreate it, run `cga migrate` from the v0.6.2 wheel, then run the seed script.
- **Verify:** console Audit tab — no row shows the yellow `legacy_unsigned` pill. Every event has either a green `verified` or grey `signed` pill.
- **Why:** DA finding — v0.5.x-era rows surface `legacy_unsigned` in the v4 console and destroy the "tamper-evident today" claim on camera.

### 2. Bundle rate limit raised for demo

- [ ] **Do:** export `GOVERNANCE_COMPLIANCE_RATE_LIMIT=100` in the shell that launches the console/backend for the shoot.
- **Verify:** `env | grep GOVERNANCE_COMPLIANCE_RATE_LIMIT` shows `100` — then **close that terminal window** (never reveal env on camera; see gotcha 8).
- **Why:** DA finding — default is 1/day. One bad take and Beat 5 errors out for 24 hours.

### 3. Pretty-printed JSON on bundle open

- [ ] **Do:** confirm the Compliance bundle endpoint returns `Content-Type: application/json` with indented output, and VSCode's default JSON formatter is on.
- **Verify:** export a throwaway bundle in a test run, open in VSCode, see 2-space indentation automatically.
- **Why:** UI/UX finding — close-up #3 relies on legibly indented JSON. Minified output kills the beat.

### 4. Throwaway AUDIT_SECRET + rotation plan post-upload

- [ ] **Do:** generate a fresh HMAC secret for this recording session only. Record the rotation window (immediately after the video is uploaded, rotate the secret and re-seed).
- **Verify:** the `AUDIT_SECRET` env var used today is NOT the one used for any other demo/staging/production.
- **Why:** Security finding — the bundle contains HMAC signatures against this secret; if it leaks and is still live, the chain is replayable.

### 5. Fresh Chrome profile

- [ ] **Do:** create a new Chrome profile named "promo-v1". Zero extensions. No bookmarks bar. No saved autofill. No tabs besides the console. DevTools closed.
- **Verify:** cmd-shift-b toggled off (no bookmarks bar); `chrome://extensions` shows empty; only one tab open.
- **Why:** UI/UX + Security findings — a CTO viewer will pause on bookmarks and read every URL. Autofill can surface unrelated company names mid-shoot.

### 6. Do Not Disturb on; notifications silenced

- [ ] **Do:** macOS Focus → Do Not Disturb on. Quit Slack, Mail, 1Password desktop, Calendar, Messages. Silence phone + put it face-down off-camera.
- **Verify:** `osascript -e 'tell application "System Events" to get the name of every process'` does not list `Slack`, `Mail`, `1Password 7`, `1Password`.
- **Why:** DA finding — a notification banner in the split-screen halt beat is a re-record.

### 7. Terminal cleared, history cleared

- [ ] **Do:** in every terminal that appears on camera, run `clear && history -c` immediately before recording that beat.
- **Verify:** `history` returns empty; visible scrollback is blank.
- **Why:** DA finding — previous commands can reveal secrets, internal hostnames, or the closed repo URL.

### 8. Never show `.env`, `env | grep`, or DevTools Application tab

- [ ] **Do:** no editor tab is open on any `.env`, `.env.local`, or similar; never run `env | grep`, `printenv`, `cat .env` on camera; Chrome DevTools is fully closed (not docked, not minimized).
- **Verify:** VSCode recent-files flyout → no `.env` entries visible; Chrome window chrome has no DevTools pane.
- **Why:** Security finding — one frame of `AUDIT_SECRET=…` on screen is a hotfix release plus an incident post-mortem.

### 9. Never show `github.com/imleopereira`

- [ ] **Do:** no browser tab, no bookmark, no terminal command references the closed source repo URL. Recent files and shell history cleared.
- **Verify:** Chrome omnibox typing `gith` does not autocomplete to `github.com/imleopereira`; shell history is cleared (see gotcha 7).
- **Why:** Marketing/brand finding — repo is private; public-video viewers clicking through hit a 404 that looks abandoned.

### 10. (Inherited from A5) No keystore-fingerprint shot

- [ ] **Do:** confirm Video 1 does not use the `governance keystore show` or drawer-fingerprint shot (that was a Script 2 concern). Leave the note in `shots.md` for Video 3.
- **Verify:** open `shots.md`, find the "Note for Video 3 only" section present; find no fingerprint reference in `voiceover.md`.
- **Why:** UI/UX + Security findings — drawer fingerprint is a salted hash not matching a base64 PEM; `governance keystore show` CLI does not exist; showing either misleads the auditor viewer.

---

## C. Final gate — 10-minute dry run

- [ ] Full audio + screen dry-run of the 90-second script, read end-to-end, no cuts.
- [ ] Playback check: every required close-up is actually visible. Every forbidden phrase is absent. The 5-second halt timer reads ≤ 5.0s on screen.
- [ ] Only after the dry-run passes cleanly: hit record for real takes.

---

## Sign-off

Recording session is go only when:
- [ ] Section A (5 gates) — all green
- [ ] Section B (10 gotchas) — all green
- [ ] Section C — dry run passed

If any box is unchecked, postpone. A broken take on camera is more expensive
than a one-day slip.
