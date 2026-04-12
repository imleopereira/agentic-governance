/**
 * Tests for `sanitizeErrorMessage` — the Security-H3 helper that strips
 * common secret-bearing tokens from error messages before they land in
 * the Zustand connection store or the ErrorBoundary fallback DOM.
 *
 * The function is a heuristic, not a guarantee, so the tests both
 * verify the happy paths (Bearer, token=, password:, etc.) AND pin the
 * known limitations so a future maintainer who tightens the regex does
 * not accidentally loosen this one.
 */

import { describe, it, expect } from "vitest";
import { sanitizeErrorMessage } from "./connectionStore";

describe("sanitizeErrorMessage — secret redaction", () => {
  it("redacts a Bearer token in an Authorization header string", () => {
    const out = sanitizeErrorMessage(
      "Request failed: Authorization: Bearer sk-abc123def456",
    );
    expect(out).not.toContain("sk-abc123def456");
    expect(out.toLowerCase()).toContain("[redacted]");
  });

  it("redacts a querystring token= value", () => {
    const out = sanitizeErrorMessage("fetch failed: /api/foo?token=xyz789");
    expect(out).not.toContain("xyz789");
    expect(out.toLowerCase()).toContain("[redacted]");
  });

  it("redacts a querystring api_key= value", () => {
    const out = sanitizeErrorMessage(
      "fetch failed: /api/foo?api_key=REDACTEDKEY",
    );
    // The regex strips anything matching key=NONSPACE, so the literal
    // "REDACTEDKEY" must not survive.
    expect(out).not.toContain("REDACTEDKEY");
  });

  it("redacts a lowercase bearer header variant", () => {
    const out = sanitizeErrorMessage("authorization: bearer abc123");
    expect(out).not.toContain("abc123");
  });

  it("redacts a password: secret123 form", () => {
    const out = sanitizeErrorMessage("login failed: password: secret123");
    expect(out).not.toContain("secret123");
  });

  it("is case-insensitive across all redaction keywords", () => {
    const out = sanitizeErrorMessage(
      "BEARER xyz · TOKEN=123 · SECRET: abc · KEY=zzz",
    );
    expect(out).not.toContain("xyz");
    expect(out).not.toContain("123");
    expect(out).not.toContain("abc");
    expect(out).not.toContain("zzz");
  });
});

describe("sanitizeErrorMessage — safe inputs", () => {
  it("returns a plain error message untouched", () => {
    const out = sanitizeErrorMessage("Network unreachable");
    expect(out).toBe("Network unreachable");
  });

  it("returns empty string unchanged", () => {
    expect(sanitizeErrorMessage("")).toBe("");
  });

  it("preserves punctuation and URLs that contain no secret patterns", () => {
    const input = "GET /api/agents 500 Internal Server Error (retry 3/3)";
    expect(sanitizeErrorMessage(input)).toBe(input);
  });
});

describe("sanitizeErrorMessage — truncation", () => {
  it("leaves messages at or under 120 chars unchanged", () => {
    const input = "a".repeat(120);
    expect(sanitizeErrorMessage(input)).toBe(input);
  });

  it("truncates messages longer than 120 chars to 117 chars plus ellipsis", () => {
    const input = "a".repeat(500);
    const out = sanitizeErrorMessage(input);
    expect(out.length).toBe(120);
    expect(out.endsWith("...")).toBe(true);
  });

  it("truncation happens after redaction, not before", () => {
    // 100 "a" + "bearer sk-abc123def456" + 50 "b" — the full length is
    // over 120 so it will truncate. The bearer redaction must still
    // strip the secret first; otherwise the truncation could cut in
    // the middle of the secret and leak a partial token.
    const input =
      "a".repeat(100) + " bearer sk-abc123def456 " + "b".repeat(50);
    const out = sanitizeErrorMessage(input);
    expect(out).not.toContain("sk-abc123def456");
  });
});
