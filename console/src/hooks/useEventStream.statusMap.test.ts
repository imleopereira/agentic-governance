/**
 * useEventStream — status-map / truncation bucketing test.
 *
 * The SSE hook assigns every envelope to a display bucket via:
 *
 *     agent_id: String(raw.agent_id ?? (raw.truncated ? "_truncated" : "unknown"))
 *
 * That single expression encodes three cases that matter for the
 * TrailPanel "by agent" filter and the DA's violation-triage screen:
 *
 *   1. Real agent_id present       → bucketed under the agent
 *   2. Missing agent_id, truncated → `_truncated` bucket (F2 fallback
 *      when the trigger payload exceeds 7 KB and sheds fields)
 *   3. Missing agent_id, no flag   → `unknown` bucket (programmer error
 *      or a corrupted NOTIFY row)
 *
 * Why not mount the hook? The console vitest env is Node-only (no jsdom,
 * no EventSource, no testing-library). Wiring the hook into a test
 * harness requires a jsdom dev dep that Cybersec dep approval has not
 * granted. We characterise the bucketing rule as a pure function and
 * pin the hook's source text so any future drift in the assignment
 * expression is caught by CI.
 *
 * File ownership note: F8 owns this test. F1 and F2 own the hook — this
 * test READS it and must never modify the hook's source.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Local re-implementation of the bucketing rule. Must stay in lockstep
// with the expression inside `useEventStream.ts :: handleMessage`.
function bucketAgent(raw: { agent_id?: unknown; truncated?: unknown }): string {
  return String(
    raw.agent_id ?? (raw.truncated ? "_truncated" : "unknown"),
  );
}

describe("useEventStream — agent bucketing rule", () => {
  it("routes events with a real agent_id to that bucket", () => {
    expect(bucketAgent({ agent_id: "agent-a" })).toBe("agent-a");
  });

  it("routes truncated envelopes without agent_id to _truncated", () => {
    expect(bucketAgent({ truncated: true })).toBe("_truncated");
  });

  it("routes non-truncated envelopes without agent_id to unknown", () => {
    expect(bucketAgent({})).toBe("unknown");
  });

  it("routes null agent_id + truncated=true to _truncated", () => {
    expect(bucketAgent({ agent_id: null, truncated: true })).toBe(
      "_truncated",
    );
  });

  it("routes null agent_id without truncation flag to unknown", () => {
    expect(bucketAgent({ agent_id: null })).toBe("unknown");
  });

  it("routes empty-string agent_id to empty string (not _truncated)", () => {
    // `??` only falls through on null/undefined, NOT on "". This behaviour
    // is subtle enough that it's worth pinning — an empty-string agent_id
    // signals a misconfigured SDK caller, not a truncated payload.
    expect(bucketAgent({ agent_id: "" })).toBe("");
  });
});

describe("useEventStream — source pin (drift guard)", () => {
  const SRC = readFileSync(
    resolve(__dirname, "./useEventStream.ts"),
    "utf8",
  );

  it("still emits the nullish-coalesce bucket expression", () => {
    // Whitespace-insensitive match for the load-bearing expression.
    const normalised = SRC.replace(/\s+/g, " ");
    expect(normalised).toContain('raw.agent_id ?? (raw.truncated ? "_truncated" : "unknown")');
  });

  it("marks hydration-deferred envelopes with _needs_hydration", () => {
    expect(SRC).toContain("_needs_hydration: true");
  });
});
