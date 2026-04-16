# Video 1 — Shot List

Camera-ready shot list, one row per voiceover beat. Matches
`voiceover.md` timestamps exactly.

## Recording spec (non-negotiable)

- Resolution: **1440p, 60 fps**.
- macOS menu bar: **hidden** (`System Settings → Control Center → Menu Bar →
  Automatically hide and show = Always`).
- Dock: hidden.
- Cursor: native macOS, default size. No highlight plugin (we want the serious
  CTO look, not a YouTube tutorial look).
- Console zoom: browser default (100%). Do **not** zoom in the browser — do it
  in post so pixels stay sharp.
- One monitor only. Second monitor disconnected so notifications cannot leak.

## Shot list

### Shot 1 — Hook card (0:00 – 0:08)

- Still card, full-bleed.
- Text: `EU AI Act · Article 12 · binding 2026-08-02`.
- Code Atelier wordmark bottom-right, 60% opacity.
- Cut at t=7.5s on first word "console".

### Shot 2 — Dashboard landing (0:08 – 0:20)

- Full console window. Tab: **Overview**.
- Must be visible:
  - Header pill: `env: demo · retention: 180 days`.
  - 4 agent tiles, exactly one red `HALTED` badge.
  - Event counter `245 events (last 24h)`.
- Cursor parked near the halted tile, motionless for the last 3s of the beat.
- **AVOID:** the `Contracts` tab in the left nav — it shows v0.7 roadmap copy
  we haven't shipped. If the nav rail is in frame, crop the recording in post
  so Contracts is cut off, or collapse the nav.
- **AVOID:** the `Filters` panel area where the time-range filter should be — v4
  nav doesn't have it yet. Don't linger on the top-right of the Audit tab.
- **DEALBREAKER — do not record if:** any agent's event-chain pill shows
  `Verifying...` state. Refresh until all pills show `verified` (green) before
  rolling.

### Shot 3 — Audit tab + verify chain (0:20 – 0:38)

- Click Audit tab (on-screen cursor move, no hard cut).
- Scroll to the key-rotation boundary (seed data places it around event ~130).
  Centered row shows `event_type: key_rotation` and `key_id` transition
  `k_2026_01 → k_2026_04` visible in the expanded detail.
- **Close-up #1 (required):** zoom on one row's `signature` column showing
  hex. Hold 2s. In post, crop + push in; do not use browser zoom.
- Hard cut to full-screen terminal:
  ```
  $ python -c "from code_atelier_governance import GovernanceSDK; \
      import asyncio; asyncio.run(GovernanceSDK(database_url='…').audit.verify_chain())"
  OK · 245/245 segments verified · chain head: 7f3a…e91c
  ```
- **Close-up #2 (required):** hold on the `OK · 245/245` line for 2s.

### Shot 4 — Halt demo (0:38 – 0:58)

- Return to console Overview (cursor moves, no hard cut).
- Hover the active agent ("agent-crawler-01" in seed data) → the drawer slides
  out on the right.
- Click `Halt agent`. Confirm dialog appears → click `Confirm halt`.
- **Split-screen composition** (prefer over A/B cuts):
  - Left half: console live event stream for that agent. Events flow, then
    stop.
  - Right half: separate Terminal window (already open pre-recording), tailing
    that agent's log:
    ```
    $ ./run_agent.sh agent-crawler-01
    … stream of ok events …
    GovernanceHaltError: agent halted by operator
    ```
- v4 console's elapsed-since-halt pill must be visible in the left half,
  stopping at a value ≤ 5.0s.
- No zoom/crop here — the 5s number is the proof, we need it legible.

### Shot 5 — Compliance export (0:58 – 1:22)

- Console → **Compliance** tab click.
- Full view of the tab: retention setting, last-export-at timestamp, and the
  big `Export Article 12 bundle` button.
- Click the button. Progress indicator → filename appears in the download tray.
- **CONTINUITY RULE (Oversight):** from the moment the download completes to
  the moment VSCode shows the pretty-printed JSON, this must be **one
  unbroken take, no cuts**. Cursor reveals the file in Finder, drags focus,
  double-clicks, window animates open, JSON renders. Continuity = the proof.
- **Close-up #3 (required):** once VSCode is open, push in on the
  `article12` top-level key showing:
  ```
  "article12": {
    "retention_days": 180,
    "chain_algorithm": "hmac-sha256",
    "signed_at": "2026-04-16T…",
    "signer_key_id": "k_2026_04",
    "generator": "code-atelier-governance/0.6.2"
  }
  ```
- Hold the close-up ~3s.

### Shot 6 — Install + end card (1:22 – 1:30)

- Fresh Terminal window. Single command pre-typed (cursor blinking at end of
  line). Leo presses Return on the word "today":
  ```
  pip install "code-atelier-governance[migrations]==0.6.2"
  ```
- First line or two of pip output visible, then fade.
- End card:
  - Code Atelier wordmark centered.
  - Subline: `codeatelier.tech/governance`.
  - No GitHub URL, no social handles, no email.

## Global AVOID list

- `.env` file open in any editor.
- `env | grep` in any terminal history.
- DevTools panel (Application/Network) visible.
- `github.com/imleopereira` in any browser tab, bookmark, or URL bar.
- Slack, Mail, 1Password, Calendar notification banners.
- Any v0.5.x-era "legacy_unsigned" yellow pill on an event row (seed must be
  fresh DB — see `prep.md`).
- Any mention of v0.6.0 or v0.6.1 install in the pip line.

## Close-up index

| # | Subject                              | Beat | Hold |
|---|--------------------------------------|------|------|
| 1 | Signature hex column on an audit row | 3    | 2s   |
| 2 | `OK · 245/245 segments verified`     | 3    | 2s   |
| 3 | `article12` block in the JSON bundle | 5    | 3s   |

All three are **required**. Missing any one = re-record.

## Note for Video 3 only (not this shoot)

The `governance keystore show` fingerprint close-up from the Script 2 draft was
cut in review. That CLI doesn't exist and the drawer's shown fingerprint is a
salted hash that won't match the base64 PEM an auditor would hold. **Video 1
does not need this shot.** Leaving the note here so the Video 3 shot list
inherits it.
