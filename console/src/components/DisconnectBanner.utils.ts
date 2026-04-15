/**
 * Pure-logic helpers extracted from DisconnectBanner.tsx so they can be
 * unit-tested without pulling JSX through the vitest transform.
 *
 * Rank table pins the F2 P0 precedence: disconnected > reconnecting >
 * connected. On a tie ``rankWorst`` returns the REST (first) argument so
 * tie-break behavior is deterministic — see DA edge case #4.
 */
export type Health = "connected" | "reconnecting" | "disconnected";

export function rankWorst(rest: Health, sse: Health): Health {
  const rank: Record<Health, number> = {
    connected: 0,
    reconnecting: 1,
    disconnected: 2,
  };
  return rank[rest] >= rank[sse] ? rest : sse;
}
