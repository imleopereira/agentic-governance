/**
 * useEventStream — reconnect-scheduling contract test.
 *
 * Characterises the exponential-backoff reconnect loop implemented in
 * `useEventStream.ts :: scheduleReconnect`. The rule set:
 *
 *   - Initial reconnect delay: 3 seconds.
 *   - Each scheduled reconnect doubles the delay, capped at 30 seconds.
 *   - A successful `onopen` resets the delay back to 3 seconds.
 *   - A manual reconnect resets the delay back to 3 seconds.
 *   - The store's `connectionStatus` transitions:
 *       connecting → connected (on open)
 *       connected  → disconnected (on error/close)
 *
 * Why not mount the hook? Same rationale as `useEventStream.statusMap.test.ts`
 * — the console vitest env is Node-only, no jsdom, no EventSource
 * polyfill. We characterise the backoff rule as a pure function and pin
 * the source text so any drift is flagged in CI. When jsdom lands as an
 * approved dev dep (tracked under the deferred-edge-cases memory),
 * these tests are the natural upgrade path to a real render-and-mock
 * EventSource harness.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Local re-implementation of the backoff rule. Must match
// `useEventStream.ts :: scheduleReconnect` / `connect.onopen`.
function nextDelay(current: number): number {
  return Math.min(current * 2, 30);
}

describe("useEventStream — reconnect backoff rule", () => {
  it("doubles 3 → 6", () => {
    expect(nextDelay(3)).toBe(6);
  });

  it("doubles 6 → 12", () => {
    expect(nextDelay(6)).toBe(12);
  });

  it("doubles 12 → 24", () => {
    expect(nextDelay(12)).toBe(24);
  });

  it("caps 24 → 30 (one step past the cap)", () => {
    expect(nextDelay(24)).toBe(30);
  });

  it("caps 30 → 30 (stable at cap)", () => {
    expect(nextDelay(30)).toBe(30);
  });

  it("never exceeds 30 even with an oversize current value", () => {
    expect(nextDelay(1000)).toBe(30);
  });
});

describe("useEventStream — reconnect source pin (drift guard)", () => {
  const SRC = readFileSync(
    resolve(__dirname, "./useEventStream.ts"),
    "utf8",
  );

  it("starts with a 3-second initial delay", () => {
    // `reconnectDelayRef.current = 3` is the canonical reset. The comment
    // on the same line calls out the "seconds, doubles on each failure"
    // rule, so pinning the literal is enough.
    expect(SRC).toMatch(/reconnectDelayRef\s*=\s*useRef\(3\)/);
  });

  it("caps backoff at 30 seconds", () => {
    expect(SRC).toContain("Math.min(reconnectDelayRef.current * 2, 30)");
  });

  it("resets to 3 seconds on successful open", () => {
    expect(SRC).toMatch(/reconnectDelayRef\.current\s*=\s*3/);
  });

  it("transitions store to connecting before attempting a new SSE", () => {
    expect(SRC).toContain('setConnectionStatus("connecting")');
  });

  it("transitions store to connected on open", () => {
    expect(SRC).toContain('setConnectionStatus("connected")');
  });

  it("transitions store to disconnected on error", () => {
    expect(SRC).toContain('setConnectionStatus("disconnected")');
  });

  it("uses Last-Event-ID replay on reconnect", () => {
    expect(SRC).toContain("last_event_id=");
  });
});
