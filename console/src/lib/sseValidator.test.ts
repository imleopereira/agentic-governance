/**
 * Tests for the hand-rolled SSE envelope validator.
 *
 * Coverage target: every conditional branch in `validateSseEnvelope`,
 * boundary values, prototype-pollution, and every required-field drop.
 */
import { describe, it, expect } from "vitest";
import { validateSseEnvelope } from "./sseValidator";

// Shared baseline envelope — individual tests spread and mutate.
const base = () => ({
  event_id: "evt-1",
  agent_id: "agent-a",
  session_id: "sess-1",
  kind: "audit",
  chain_seq: 42,
  created_at: "2026-04-15T12:00:00.000Z",
});

describe("validateSseEnvelope — happy path", () => {
  it("accepts a valid envelope", () => {
    const out = validateSseEnvelope(base());
    expect(out).not.toBeNull();
    expect(out?.event_id).toBe("evt-1");
    expect(out?.chain_seq).toBe(42);
    expect(out?.session_id).toBe("sess-1");
    expect(out?.truncated).toBeUndefined();
  });

  it("accepts a truncated-fallback envelope (session_id omitted)", () => {
    const out = validateSseEnvelope({
      event_id: "evt-big",
      agent_id: "agent-a",
      kind: "audit",
      chain_seq: 99,
      created_at: "2026-04-15T12:00:00.000Z",
      truncated: true,
    });
    expect(out?.truncated).toBe(true);
    expect(out?.session_id).toBeNull();
  });

  it("accepts session_id explicitly null", () => {
    const out = validateSseEnvelope({ ...base(), session_id: null });
    expect(out?.session_id).toBeNull();
  });
});

describe("validateSseEnvelope — non-object inputs", () => {
  it("rejects null", () => {
    expect(validateSseEnvelope(null)).toBeNull();
  });

  it("rejects undefined", () => {
    expect(validateSseEnvelope(undefined)).toBeNull();
  });

  it("rejects a raw string", () => {
    expect(validateSseEnvelope("hello")).toBeNull();
  });

  it("rejects a raw number", () => {
    expect(validateSseEnvelope(42)).toBeNull();
  });

  it("rejects an array", () => {
    expect(validateSseEnvelope([1, 2, 3])).toBeNull();
  });
});

describe("validateSseEnvelope — missing required fields", () => {
  it("rejects when event_id is missing", () => {
    const { event_id: _e, ...rest } = base();
    expect(validateSseEnvelope(rest)).toBeNull();
  });

  it("rejects when agent_id is missing", () => {
    const { agent_id: _a, ...rest } = base();
    expect(validateSseEnvelope(rest)).toBeNull();
  });

  it("rejects when kind is missing", () => {
    const { kind: _k, ...rest } = base();
    expect(validateSseEnvelope(rest)).toBeNull();
  });

  it("rejects when chain_seq is missing", () => {
    const { chain_seq: _c, ...rest } = base();
    expect(validateSseEnvelope(rest)).toBeNull();
  });

  it("rejects when created_at is missing", () => {
    const { created_at: _t, ...rest } = base();
    expect(validateSseEnvelope(rest)).toBeNull();
  });
});

describe("validateSseEnvelope — wrong types", () => {
  it("rejects numeric event_id", () => {
    expect(validateSseEnvelope({ ...base(), event_id: 1 })).toBeNull();
  });

  it("rejects string chain_seq", () => {
    expect(validateSseEnvelope({ ...base(), chain_seq: "42" })).toBeNull();
  });

  it("rejects float chain_seq", () => {
    expect(validateSseEnvelope({ ...base(), chain_seq: 1.5 })).toBeNull();
  });

  it("rejects negative chain_seq", () => {
    expect(validateSseEnvelope({ ...base(), chain_seq: -1 })).toBeNull();
  });

  it("rejects NaN chain_seq", () => {
    expect(validateSseEnvelope({ ...base(), chain_seq: NaN })).toBeNull();
  });

  it("rejects -Infinity chain_seq", () => {
    expect(
      validateSseEnvelope({ ...base(), chain_seq: -Infinity }),
    ).toBeNull();
  });

  it("rejects unparseable created_at", () => {
    expect(
      validateSseEnvelope({ ...base(), created_at: "not-a-date" }),
    ).toBeNull();
  });
});

describe("validateSseEnvelope — boundary and oversized strings", () => {
  it("rejects empty event_id", () => {
    expect(validateSseEnvelope({ ...base(), event_id: "" })).toBeNull();
  });

  it("rejects oversized kind (> 4 KB)", () => {
    const big = "x".repeat(4097);
    expect(validateSseEnvelope({ ...base(), kind: big })).toBeNull();
  });
});

describe("validateSseEnvelope — prototype pollution", () => {
  it("rejects an envelope with a literal __proto__ key", () => {
    // Build via JSON parse so the key lands as an own-property, not the
    // actual prototype slot — this is the realistic attacker payload.
    const hostile = JSON.parse(
      '{"event_id":"e","agent_id":"a","session_id":"s","kind":"audit","chain_seq":1,"created_at":"2026-04-15T12:00:00.000Z","__proto__":{"polluted":true}}',
    );
    expect(validateSseEnvelope(hostile)).toBeNull();
  });

  it("rejects an envelope with a constructor key", () => {
    const hostile = { ...base(), constructor: "hostile" };
    expect(validateSseEnvelope(hostile)).toBeNull();
  });
});
