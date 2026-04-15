/**
 * F1 Console Honesty Pass — loading-state contract across drill panels.
 *
 * Each panel's `isLoading` branch must render `<Skeleton>`, not
 * `<EmptyState>`. Source-level grep keeps the test framework-free:
 * the console package does not ship jsdom / @testing-library, so we
 * pin the contract at the file-content layer. The test will break
 * immediately if a panel reverts to the old "Loading …" EmptyState
 * pattern.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

function read(name: string): string {
  return readFileSync(join(__dirname, "drill", name), "utf8");
}

describe.each([
  ["BudgetPanel.tsx"],
  ["SessionsPanel.tsx"],
  ["TrailPanel.tsx"],
])("%s isLoading branch", (name) => {
  const src = read(name);

  it("imports Skeleton", () => {
    expect(src).toMatch(/from\s+"@\/components\/Skeleton"/);
  });

  it("renders <Skeleton/> when isLoading is true", () => {
    // Extract the isLoading if-block and assert it contains Skeleton
    // and does NOT contain EmptyState.
    const match = src.match(/if \(isLoading\) \{([\s\S]*?)\n {2}\}/);
    expect(match, "isLoading branch not found").toBeTruthy();
    const branch = match?.[1] ?? "";
    expect(branch).toContain("<Skeleton");
    expect(branch).not.toContain("<EmptyState");
  });

  it("sets aria-busy on the loading container", () => {
    const match = src.match(/if \(isLoading\) \{([\s\S]*?)\n {2}\}/);
    const branch = match?.[1] ?? "";
    expect(branch).toContain('aria-busy="true"');
  });
});

describe("SessionsPanel title", () => {
  it('uses the "Spend by session" label via SESSIONS_PANEL_TITLE', () => {
    const src = read("SessionsPanel.tsx");
    expect(src).toContain("SESSIONS_PANEL_TITLE");
    // And the constant itself resolves to "Spend by session":
    const emptySrc = readFileSync(
      join(__dirname, "..", "..", "lib", "empty-states.ts"),
      "utf8",
    );
    expect(emptySrc).toContain('"Spend by session"');
  });
});
