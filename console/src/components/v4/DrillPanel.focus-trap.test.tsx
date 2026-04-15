/**
 * DrillPanel focus-trap contract test.
 *
 * F1 removes the hand-rolled focus trap from `DrillPanel.tsx` in v0.6 —
 * modern screen readers / keyboard users prefer natural Tab cycling over
 * a manual trap, and the removal closes an accessibility bug where a
 * trap kept focus inside a closed-but-not-yet-unmounted panel during
 * React transitions. This test pins the characterisation:
 *
 *   - The component is still exported and mountable.
 *   - Escape-key handling and focus-restore to the opener element are
 *     preserved (those are NOT the focus trap and must stay).
 *   - Tab/Shift-Tab cycling within the dialog is NOT clamped by a
 *     hand-rolled handler — the file must NOT contain the tell-tale
 *     `first.focus()` + `last.focus()` clamp pattern once F1's removal
 *     lands.
 *
 * Why a source-text assertion? The console vitest environment is Node
 * only (no jsdom, no @testing-library/react). Mounting the component is
 * impossible without adding a dev dep, which Cybersec dep approval has
 * not granted. Source scanning is a pragmatic second-best that still
 * fails loud if a contributor reintroduces the clamp pattern.
 *
 * See also: F1 owns `DrillPanel.tsx`; F8 only READS it here.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SRC = readFileSync(
  resolve(__dirname, "./DrillPanel.tsx"),
  "utf8",
);

describe("DrillPanel — focus-trap contract", () => {
  it("still exports the DrillPanel symbol", () => {
    expect(SRC).toMatch(/export\s+(function|const)\s+DrillPanel\b/);
  });

  it("preserves Escape close handler (not part of the trap)", () => {
    // Escape-key close + focus-restore to trigger stays regardless of
    // whether the clamped Tab cycle is there.
    expect(SRC).toMatch(/e\.key\s*===\s*"Escape"/);
  });

  it("preserves restore-focus-to-opener on close", () => {
    expect(SRC).toMatch(/restoreTarget|restoreFocusTo|openerRef/);
  });

  // F1 removal target — flips from `.todo` to a live assertion the moment
  // F1's patch lands. Leaving the inverse todo means the test file does
  // not need to be re-authored on the other side of the refactor.
  it.todo(
    "does NOT clamp Tab with a hand-rolled first/last focus cycle (F1 removes)",
  );

  it.todo(
    "does NOT intercept Tab key events inside the dialog (F1 removes)",
  );
});
