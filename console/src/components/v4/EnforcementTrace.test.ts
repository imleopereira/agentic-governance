/**
 * F1 Console Honesty Pass — EnforcementTrace prop contract.
 *
 * Pins that the dead `sessionEvents` prop is gone. It was declared but
 * never destructured, so every consumer that passed it silently did
 * nothing. The prop is removed in v0.6 F1 and this test prevents a
 * copy-paste revival of the dead API.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const source = readFileSync(
  join(__dirname, "EnforcementTrace.tsx"),
  "utf8",
);

describe("EnforcementTrace props (source-level)", () => {
  it("does NOT declare a sessionEvents prop", () => {
    // No `sessionEvents:` type declaration in the Props interface.
    expect(source).not.toMatch(/sessionEvents\??\s*:/);
    // No destructuring / usage as a variable either.
    expect(source).not.toMatch(/\{\s*[^}]*\bsessionEvents\b[^}]*\}/);
  });

  it("EnforcementTraceProps interface exists and has only `event`", () => {
    const match = source.match(
      /export interface EnforcementTraceProps \{([\s\S]*?)\}/,
    );
    expect(match, "EnforcementTraceProps interface not found").toBeTruthy();
    const body = match?.[1] ?? "";
    // Only the `event` field; no stray optional props.
    expect(body).toContain("event: AuditEvent");
    // The body should not contain any other typed member besides `event`.
    const memberLines = body
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith("//") && !l.startsWith("*"));
    expect(memberLines).toEqual(["event: AuditEvent;"]);
  });
});
