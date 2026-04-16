# Video 1 — Voiceover Script

**Target buyer:** CTO / Head of Security / Compliance Officer at a company shipping
AI agents into production.

**Total runtime:** 90 seconds.
**Word budget:** ~95 words (≈64 wpm, deliberate CTO-buyer cadence).
**Delivery:** serious, measured, never excitable. Read as if you were walking an
auditor through evidence — not pitching a product.

Two breathing pauses are explicitly marked. Do not skip them.

---

## Beat 1 — Hook (0:00 – 0:08) — 8s

**Voiceover (20 words):**
"EU AI Act Article 12 becomes binding on August second. Every agent action must
be logged — six months retained, tamper-evident."

**On-screen:**
- Full-bleed still card: "EU AI Act · Article 12 · binding 2026-08-02".
- No UI chrome. Dark background. Small Code Atelier wordmark bottom-right.
- Cut to console login (pre-logged-in) on last beat word.

---

## Beat 2 — Dashboard landing (0:08 – 0:20) — 12s

**Voiceover (17 words):**
"This is the governance console against a real agent fleet. Four agents. One
currently halted. Two-hundred-forty-five events."

**[breathing pause — 0.5s — beat of silence before moving to audit]**

**On-screen:**
- Console overview page, fully loaded.
- Header pill shows "env: demo · retention: 180 days".
- Agent cards: 4 tiles visible, one with the red "HALTED" badge.
- Event counter top-right shows "245 events (last 24h)".
- Cursor rests near the halted agent tile — does not click yet.

---

## Beat 3 — Audit trail proof (0:20 – 0:38) — 18s

**Voiceover (25 words):**
"Every event is HMAC-chained. Tamper-evident HMAC chain with on-demand
verification via `sdk.audit.verify_chain()`. One key rotation shows in history —
the chain stays continuous across it."

**On-screen:**
- Click the Audit tab.
- Scroll so the key-rotation boundary event is centered (timestamp + `key_id`
  transition visible in the row detail pane).
- Close-up #1: zoom the signature hex column of one row for ~2 seconds.
- Cut to terminal: `python -c "…verify_chain()…"` running, prints `OK · 245/245
  segments verified`.
- Close-up #2: that terminal line, held for 2 seconds.

---

## Beat 4 — Halt demo (0:38 – 0:58) — 20s

**Voiceover (22 words):**
"Five-second halt across every enforcement path, including outbound LLM
calls — no redeploy, no config push, no message bus. Watch the agent go
quiet."

**[breathing pause — 0.5s — before the halt click]**

**On-screen:**
- Back to console. Hover the halted agent tile, then switch to a currently
  active one.
- Click "Halt agent" in the action drawer. Confirm dialog → confirm.
- Split-screen (or quick A/B cut):
  - Left: agent's live event stream in the console — events flowing, then stop.
  - Right: the agent's own terminal tailing its logs — next tool call returns
    `GovernanceHaltError` within 5 seconds.
- On-screen timer pill (already in v4 console) shows elapsed since halt, stops
  at a value ≤ 5.0s.

---

## Beat 5 — Evidence bundle (0:58 – 1:22) — 24s

**Voiceover (22 words):**
"Signed, self-describing Article 12 evidence bundle — hand-off artifact for
internal audit and design-partner demos today. One click. Pretty-printed,
readable JSON."

**On-screen:**
- Console → Compliance tab.
- Click "Export Article 12 bundle" button.
- Progress indicator → download completes.
- Cursor reveals the file in Finder (single continuous move, no cut).
- Double-click the `.json` file — opens in VSCode, already pretty-printed.
- Close-up #3: hold on the `article12` top-level key showing
  `retention_days: 180`, `chain_algorithm: "hmac-sha256"`, signed metadata
  block. Hold ~3 seconds.

---

## Beat 6 — Install + close (1:22 – 1:30) — 8s

**Voiceover (9 words):**
"One pip install. Your Postgres. Audit-ready today."

**On-screen:**
- Clean terminal, single command pre-typed (Leo presses return on "today"):
  ```
  pip install "code-atelier-governance[migrations]==0.6.2"
  ```
- Fade to end card: "Code Atelier Governance · codeatelier.tech/governance".
- No GitHub URL. No social handles. Wordmark only.

---

## Word-count budget check

| Beat | Words |
|------|-------|
| 1    | 20    |
| 2    | 17    |
| 3    | 25    |
| 4    | 22    |
| 5    | 22    |
| 6    | 9     |
| **Total** | **115** |

Trim target if Leo's cadence runs long: Beat 5 "and design-partner demos today"
→ "for internal audit." (saves 5 words, keeps "hand-off artifact" required
phrase).

---

## Phrases explicitly NOT in this script (do not add back)

- "Anthropic ARS"
- "NIST requires reasoning + rejected alternatives"
- "Finland enforced EU AI Act January 2026"
- "60 seconds from pip install to evidence"
- "chain integrity verifiable offline"
- "signed evidence bundle for the regulator"
- "halt fires instantly"
- "rotation-safe" (bare, without qualifier)

If a take drifts into any of those, cut and re-record the beat.
