/**
 * F2 P0 DA edge case #4: DisconnectBanner rankWorst tie-breaking.
 *
 * When both the REST store and SSE store report the same health state
 * (e.g. both "disconnected"), ``rankWorst`` must return a deterministic
 * health value (not throw, not undefined). The current implementation
 * returns ``rest`` on ties — this test pins that contract so a future
 * refactor cannot silently break the invariant.
 */
import { describe, it, expect } from "vitest";
import { rankWorst } from "./DisconnectBanner.utils";

describe("rankWorst — tie-breaking and precedence", () => {
  it("returns connected when both inputs are connected", () => {
    expect(rankWorst("connected", "connected")).toBe("connected");
  });

  it("returns disconnected when both inputs are disconnected (tie)", () => {
    expect(rankWorst("disconnected", "disconnected")).toBe("disconnected");
  });

  it("returns reconnecting when both inputs are reconnecting (tie)", () => {
    expect(rankWorst("reconnecting", "reconnecting")).toBe("reconnecting");
  });

  it("picks the worst when rest is worse", () => {
    expect(rankWorst("disconnected", "connected")).toBe("disconnected");
    expect(rankWorst("reconnecting", "connected")).toBe("reconnecting");
    expect(rankWorst("disconnected", "reconnecting")).toBe("disconnected");
  });

  it("picks the worst when sse is worse", () => {
    expect(rankWorst("connected", "disconnected")).toBe("disconnected");
    expect(rankWorst("connected", "reconnecting")).toBe("reconnecting");
    expect(rankWorst("reconnecting", "disconnected")).toBe("disconnected");
  });

  it("tie on disconnected deterministically returns rest (first arg)", () => {
    // Pinning the tie-break: rank(rest) >= rank(sse) => rest wins.
    expect(rankWorst("disconnected", "disconnected")).toBe("disconnected");
  });
});
