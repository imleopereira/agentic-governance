/**
 * Tests for the runtime metadata-field validators.
 *
 * One test per unsafe coercion site currently in `useEventStream.ts` /
 * `EnforcementTrace.tsx` (6 total). Together they pin the conservative
 * narrowing contract: only bounded non-empty strings pass through.
 */
import { describe, it, expect } from "vitest";
import { validateMetadata } from "./metadataValidator";

describe("validateMetadata — 6 unsafe coercion sites", () => {
  it("coercion 1: model → passes a normal string", () => {
    expect(validateMetadata({ model: "gpt-4" }).model).toBe("gpt-4");
  });

  it("coercion 2: model → rejects a numeric", () => {
    expect(validateMetadata({ model: 42 }).model).toBeNull();
  });

  it("coercion 3: tool → passes a normal string", () => {
    expect(validateMetadata({ tool: "shell.run" }).tool).toBe("shell.run");
  });

  it("coercion 4: tool → rejects an object", () => {
    expect(validateMetadata({ tool: { nested: true } }).tool).toBeNull();
  });

  it("coercion 5: request_id → passes a UUID-ish string", () => {
    expect(
      validateMetadata({ request_id: "req-1234" }).request_id,
    ).toBe("req-1234");
  });

  it("coercion 6: request_id → rejects an oversized string", () => {
    const big = "x".repeat(2000);
    expect(
      validateMetadata({ request_id: big }).request_id,
    ).toBeNull();
  });

  it("null input returns all-null", () => {
    const r = validateMetadata(null);
    expect(r).toEqual({ model: null, tool: null, request_id: null });
  });

  it("undefined input returns all-null", () => {
    const r = validateMetadata(undefined);
    expect(r).toEqual({ model: null, tool: null, request_id: null });
  });

  it("empty-string fields are rejected (drift guard)", () => {
    const r = validateMetadata({ model: "", tool: "", request_id: "" });
    expect(r).toEqual({ model: null, tool: null, request_id: null });
  });

  // DA Wave 4 edge cases -----------------------------------------------

  it("rejects a model longer than the 1024-char cap", () => {
    const big = "a".repeat(1025);
    expect(validateMetadata({ model: big }).model).toBeNull();
  });

  it("empty object yields all-null", () => {
    expect(validateMetadata({})).toEqual({
      model: null,
      tool: null,
      request_id: null,
    });
  });
});
