/**
 * F1 Console Honesty Pass — DrillPanel contract pins.
 *
 * These tests are pure TS-level assertions; they do NOT render React.
 * The console package does not ship with @testing-library/react or
 * jsdom in devDependencies, so DOM-level tests are deferred until that
 * surface lands. Until then we pin:
 *
 *   1. TABS length === 4 (no Contracts tab).
 *   2. DrillPanel.tsx uses role="complementary", NOT role="dialog".
 *   3. DrillPanel.tsx does not contain a focus-trap loop (no FOCUSABLE
 *      querySelector shift-tab cycling).
 *   4. Escape-close handler is still present.
 *   5. No "Halt (v0.6)" disabled button.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const source = readFileSync(
  join(__dirname, "DrillPanel.tsx"),
  "utf8",
);

// Parse the `TABS` literal out of source so we don't have to import
// the .tsx file (Vitest default transform parses .ts as non-JSX).
function parseTabIds(src: string): string[] {
  const match = src.match(/export const TABS[^=]*=\s*\[([\s\S]*?)\]\s*;/);
  if (!match) return [];
  const ids: string[] = [];
  const re = /id:\s*"([^"]+)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(match[1])) !== null) ids.push(m[1]);
  return ids;
}
const tabIds = parseTabIds(source);

describe("DrillPanel TABS array", () => {
  it("has exactly 4 tabs", () => {
    expect(tabIds).toHaveLength(4);
  });

  it("does not include Contracts", () => {
    expect(tabIds).not.toContain("contracts");
  });

  it("is exactly trail, scope, budget, sessions", () => {
    expect(tabIds).toEqual(["trail", "scope", "budget", "sessions"]);
  });
});

describe("DrillPanel a11y contracts (source-level)", () => {
  it("uses role=complementary, not role=dialog", () => {
    // Strip doc-comment lines before grepping: the comment explains the
    // v0.6 change and mentions the old role by name.
    const codeOnly = source
      .split("\n")
      .filter((line) => !/^\s*\*/.test(line))
      .join("\n");
    expect(codeOnly).toContain('role="complementary"');
    expect(codeOnly).not.toContain('role="dialog"');
  });

  it("renders an <aside> element", () => {
    expect(source).toMatch(/<aside\b/);
  });

  it("does not contain a Tab-cycling focus trap", () => {
    // The removed focus trap iterated focusables and called first.focus() /
    // last.focus() on Shift-Tab. If either call returns, the trap is back.
    expect(source).not.toContain("last.focus()");
    expect(source).not.toMatch(/first\.focus\(\)\s*$/m);
  });

  it("still closes on Escape", () => {
    expect(source).toContain('e.key === "Escape"');
    expect(source).toContain("onClose()");
  });

  it("does NOT render a Halt (v0.6) disabled button", () => {
    // Strip comments (leading " * ..." lines) before asserting, since the
    // doc comment does reference the removed button by name.
    const codeOnly = source
      .split("\n")
      .filter((line) => !/^\s*\*/.test(line))
      .join("\n");
    expect(codeOnly).not.toMatch(/>Halt \(v0\.6\)</);
    expect(codeOnly).not.toContain("HALT_TOOLTIP");
  });
});
