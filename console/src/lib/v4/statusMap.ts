/**
 * mapAgentStatus — single source of truth for `PostureAgent` →
 * `StatusKind` rendering used by the v4 agents list and the drill panel
 * header.
 *
 * The backend posture endpoint (`/api/posture`, see app.py:1380-1433)
 * emits four module health signals as `"PASS" | "WARN" | "FAIL"`:
 * scope, cost, gates, audit. That's enough to surface active failures,
 * but it does not distinguish "running right now" from "registered
 * months ago, inactive since". Without a liveness signal, every
 * healthy agent renders as a pulsing green "Running" dot — even if it
 * hasn't made a call in a week.
 *
 * Fix: layer a liveness heuristic on top of the health signals:
 *   - `event_count === 0` OR `last_active === null` → "ready"
 *     (registered, never active)
 *   - `last_active` older than IDLE_THRESHOLD_MS → "ready"
 *     (dormant; the agent is healthy but not actively running)
 *   - within IDLE_THRESHOLD_MS AND any FAIL/WARN/pending → apply
 *     existing priority (halted > failed > degraded > blocked)
 *   - within IDLE_THRESHOLD_MS AND all PASS → "running" (pulsing dot)
 *
 * IDLE_THRESHOLD_MS is 15 seconds — aligned with the 5-second list
 * refetch cadence, allowing one missed cycle before a live agent flips
 * to "idle". See the constant for the full rationale. Callers can
 * override via the `now` argument for testing.
 *
 * Priority when live: audit FAIL (halted) > scope FAIL (failed) >
 * cost FAIL (degraded — bleeding money) > gates.pending (blocked) >
 * any WARN (degraded) > all PASS (running). Unknown status values
 * surface as "degraded" (fail-loud, never silent green).
 */

import type { PostureAgent } from "@/lib/api";
import type { StatusKind } from "@/components/v4/StatusDot";

const _unknownStatusWarned = new Set<string>();

// 15 seconds. The list refetches every 5s, so a "live" agent should
// have its last_active bumped on every refetch cycle. 15s gives room
// for one missed cycle (network blip, slow backend) before the dot
// flips from pulsing green "Running" to static blue "Idle". Earlier
// versions used 2 minutes which was too permissive — the test backend
// updates last_active on cost-tracking and heartbeats too, so every
// agent looked live.
const IDLE_THRESHOLD_MS = 15 * 1000;

export function mapAgentStatus(
  agent: PostureAgent,
  now: number = Date.now(),
): StatusKind {
  const s = agent.scope.status;
  const c = agent.cost.status;
  const a = agent.audit.status;

  // Validate backend status literals.
  const known = new Set(["PASS", "WARN", "FAIL"]);
  if (!known.has(s) || !known.has(c) || !known.has(a)) {
    const key = `${s}|${c}|${a}`;
    if (!_unknownStatusWarned.has(key)) {
      _unknownStatusWarned.add(key);
      // eslint-disable-next-line no-console
      console.warn("mapAgentStatus: unknown status", {
        scope: s,
        cost: c,
        audit: a,
      });
    }
    return "degraded";
  }

  // Hard-fail states always win regardless of liveness — a halted agent
  // is halted even if last_active is stale.
  if (a === "FAIL") return "halted";
  if (s === "FAIL") return "failed";

  // Liveness check: agents that have never been active OR have been
  // idle longer than the threshold are "ready", not "running". A
  // malformed `last_active` (Date.parse → NaN) is treated as stale so
  // a drifted backend schema never silently promotes an inactive agent
  // to a pulsing green "Running" dot.
  const neverActive =
    agent.event_count === 0 || agent.last_active === null;
  let isLive = !neverActive;
  if (isLive && agent.last_active) {
    const lastActiveMs = Date.parse(agent.last_active);
    if (Number.isNaN(lastActiveMs)) {
      isLive = false;
    } else {
      isLive = now - lastActiveMs <= IDLE_THRESHOLD_MS;
    }
  }

  if (!isLive) return "ready";

  // Live path: apply remaining priority chain.
  if (c === "FAIL") return "degraded";
  if (agent.gates.pending > 0) return "blocked";
  if (s === "WARN" || c === "WARN" || a === "WARN") return "degraded";
  if (s === "PASS" && c === "PASS" && a === "PASS" && agent.gates.pending === 0) {
    return "running";
  }
  return "degraded";
}
