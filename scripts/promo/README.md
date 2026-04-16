# Code Atelier Governance — Promo Video Program

Three planned videos. Shipped in strict order. Each video's trigger is a
signal from the previous one; we do not start a later video's recording until
its trigger fires.

## Video 1 — 90s Article 12 walkthrough (ships FIRST)

- **Directory:** `video1_article12/`
- **Audience:** CTO / Head of Security / Compliance Officer
- **Owner:** Director (script + shot list) + Leo (on-camera) + Seed Builder agent (demo data).
- **Why first (CEO verdict):** Article 12 becomes binding 2026-08-02. Compliance
  officers and CTOs own the budget. This video converts urgency into a
  qualified call. The 45s hero becomes a recut from this footage, not a
  separate shoot.
- **Ship trigger:** all 5 DA unblock gates in `video1_article12/prep.md`
  section A are green — v0.6.2 on PyPI, clean-install QA green, seed script
  green, rollback narrative reconciled, keystore shot confirmed-cut.

## Video 2 — 45s hero recut (ships SECOND)

- **Directory:** `video2_hero/` (to be created when trigger fires)
- **Audience:** top-of-funnel engineering leader, HN/Twitter, docs homepage hero.
- **Owner:** Marketing (script) + Director (editor, reusing Video 1 b-roll).
- **Source material:** recut of Video 1 footage. No new recording.
- **Ship trigger:** Video 1 has 7 days of retention data (watch-through % on
  the docs-site embed and LinkedIn post). We use that signal to pick which
  beats of Video 1 to keep and which to cut for a 45s reshape.

## Video 3 — 2min enforcement-vs-observability technical (ships THIRD)

- **Directory:** `video3_enforcement_vs_observability/` (to be created when trigger fires)
- **Audience:** senior eng / platform leads on HN + Twitter; posted alongside
  a long-form blog.
- **Owner:** Director + Leo (on-camera).
- **Content:** side-by-side with an observability-only stack (LangSmith /
  Langfuse / Helicone), showing a scope-violation or budget-overrun event
  that they log and we block.
- **Ship trigger:** 2 design partners have signed (not just verbally
  committed). Design-partner names and use-cases unlock the credibility to
  post a technical teardown without it reading as theoretical.
- **Note for future Director:** the keystore-fingerprint shot from the
  original Script 2 draft was cut in review (no `governance keystore show`
  CLI; drawer fingerprint is a salted hash not matching a base64 PEM). See
  `video1_article12/shots.md` bottom section. Do not reintroduce.

## Program rules (apply to all 3 videos)

- Forbidden phrase list (see `video1_article12/voiceover.md` bottom section)
  applies to every video, not just Video 1.
- Every video's `prep.md` must pass fully before its recording day.
- No video links `github.com/imleopereira` (closed repo).
- Install line is **always** `pip install "code-atelier-governance[migrations]==0.6.2"`
  (or whatever stable version is live at record time — never the broken
  0.6.0/0.6.1 wheels).
- Demo DB is always fresh-dropped before recording.
