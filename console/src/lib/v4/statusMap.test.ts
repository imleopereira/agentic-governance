/**
 * Tests for `mapAgentStatus` — the v4 agents list status mapper.
 *
 * This function is the single source of truth for how the console
 * visualises agent health, so the test suite exhaustively covers:
 *   - hard-fail precedence (audit/scope FAIL win over liveness)
 *   - liveness thresholds (15 s window, stale → idle, null → idle)
 *   - WARN propagation to "degraded"
 *   - pending HITL gates → "blocked"
 *   - unknown status literals → "degraded" (fail-loud, never silent green)
 *
 * Tests fix `now` explicitly via the second argument so they are not
 * time-dependent.
 */

import { describe, it, expect } from "vitest";
import { mapAgentStatus } from "./statusMap";
import type { PostureAgent } from "@/lib/api";

// Fixed "now" used by every test: 2026-04-12T12:00:00.000Z.
const NOW = Date.parse("2026-04-12T12:00:00.000Z");
const SECONDS_AGO = (n: number) =>
  new Date(NOW - n * 1000).toISOString();

// Helper: build a baseline PostureAgent with all-PASS and recent activity.
// Individual tests override fields via the partial argument.
function agent(overrides: Partial<PostureAgent> = {}): PostureAgent {
  return {
    agent_id: "test-agent",
    event_count: 100,
    last_active: SECONDS_AGO(5), // well within 15s liveness window
    scope: {
      status: "PASS",
      violations_today: 0,
      latest_violation: null,
    },
    cost: {
      status: "PASS",
      usd_today: 0.5,
      tokens_today: 1000,
      exceeded_today: 0,
    },
    gates: { status: "PASS", pending: 0 },
    audit: { status: "PASS", events_total: 100 },
    ...overrides,
  };
}

describe("mapAgentStatus — happy path", () => {
  it("returns running when agent is live and all modules are PASS", () => {
    expect(mapAgentStatus(agent(), NOW)).toBe("running");
  });
});

describe("mapAgentStatus — liveness", () => {
  it("returns ready when event_count is 0", () => {
    expect(mapAgentStatus(agent({ event_count: 0 }), NOW)).toBe("ready");
  });

  it("returns ready when last_active is null", () => {
    expect(mapAgentStatus(agent({ last_active: null }), NOW)).toBe("ready");
  });

  it("returns running when last_active is exactly 15 s ago (boundary)", () => {
    expect(
      mapAgentStatus(agent({ last_active: SECONDS_AGO(15) }), NOW),
    ).toBe("running");
  });

  it("returns ready when last_active is 16 s ago (just past boundary)", () => {
    expect(
      mapAgentStatus(agent({ last_active: SECONDS_AGO(16) }), NOW),
    ).toBe("ready");
  });

  it("returns ready when last_active is an hour ago", () => {
    expect(
      mapAgentStatus(agent({ last_active: SECONDS_AGO(3600) }), NOW),
    ).toBe("ready");
  });

  it("treats malformed last_active as idle (NaN parse → false liveness)", () => {
    expect(
      mapAgentStatus(agent({ last_active: "not-a-date" }), NOW),
    ).toBe("ready");
  });
});

describe("mapAgentStatus — hard fail precedence", () => {
  it("returns halted when audit.status is FAIL, even if stale", () => {
    expect(
      mapAgentStatus(
        agent({
          audit: { status: "FAIL", events_total: 100 },
          last_active: SECONDS_AGO(3600),
        }),
        NOW,
      ),
    ).toBe("halted");
  });

  it("returns halted when audit.status is FAIL, even if event_count is 0", () => {
    expect(
      mapAgentStatus(
        agent({
          audit: { status: "FAIL", events_total: 0 },
          event_count: 0,
        }),
        NOW,
      ),
    ).toBe("halted");
  });

  it("returns failed when scope.status is FAIL (and audit is OK)", () => {
    expect(
      mapAgentStatus(
        agent({
          scope: {
            status: "FAIL",
            violations_today: 3,
            latest_violation: null,
          },
        }),
        NOW,
      ),
    ).toBe("failed");
  });

  it("returns failed when scope.status is FAIL, even if stale", () => {
    expect(
      mapAgentStatus(
        agent({
          scope: {
            status: "FAIL",
            violations_today: 3,
            latest_violation: null,
          },
          last_active: SECONDS_AGO(3600),
        }),
        NOW,
      ),
    ).toBe("failed");
  });

  it("audit FAIL takes priority over scope FAIL", () => {
    expect(
      mapAgentStatus(
        agent({
          audit: { status: "FAIL", events_total: 100 },
          scope: {
            status: "FAIL",
            violations_today: 3,
            latest_violation: null,
          },
        }),
        NOW,
      ),
    ).toBe("halted");
  });
});

describe("mapAgentStatus — degraded / blocked (live branch)", () => {
  it("returns degraded when cost.status is FAIL but agent is live", () => {
    expect(
      mapAgentStatus(
        agent({
          cost: {
            status: "FAIL",
            usd_today: 10,
            tokens_today: 50000,
            exceeded_today: 1,
          },
        }),
        NOW,
      ),
    ).toBe("degraded");
  });

  it("returns blocked when gates.pending > 0 and all modules PASS", () => {
    expect(
      mapAgentStatus(
        agent({
          gates: { status: "WARN", pending: 1 },
        }),
        NOW,
      ),
    ).toBe("blocked");
  });

  it("returns degraded when any module is WARN (scope)", () => {
    expect(
      mapAgentStatus(
        agent({
          scope: {
            status: "WARN",
            violations_today: 0,
            latest_violation: null,
          },
        }),
        NOW,
      ),
    ).toBe("degraded");
  });

  it("returns degraded when any module is WARN (cost)", () => {
    expect(
      mapAgentStatus(
        agent({
          cost: {
            status: "WARN",
            usd_today: 3,
            tokens_today: 10000,
            exceeded_today: 0,
          },
        }),
        NOW,
      ),
    ).toBe("degraded");
  });

  it("cost FAIL takes precedence over gates.pending (live branch)", () => {
    expect(
      mapAgentStatus(
        agent({
          cost: {
            status: "FAIL",
            usd_today: 10,
            tokens_today: 50000,
            exceeded_today: 1,
          },
          gates: { status: "WARN", pending: 5 },
        }),
        NOW,
      ),
    ).toBe("degraded");
  });
});

describe("mapAgentStatus — stale agents bypass live-only branches", () => {
  it("returns ready for stale agent with pending gates (gates don't promote stale to blocked)", () => {
    expect(
      mapAgentStatus(
        agent({
          last_active: SECONDS_AGO(120),
          gates: { status: "WARN", pending: 2 },
        }),
        NOW,
      ),
    ).toBe("ready");
  });

  it("returns ready for stale agent with cost WARN (WARNs don't promote stale to degraded)", () => {
    expect(
      mapAgentStatus(
        agent({
          last_active: SECONDS_AGO(120),
          cost: {
            status: "WARN",
            usd_today: 3,
            tokens_today: 10000,
            exceeded_today: 0,
          },
        }),
        NOW,
      ),
    ).toBe("ready");
  });
});

describe("mapAgentStatus — unknown status literals", () => {
  it("returns degraded when scope.status is an unknown string", () => {
    expect(
      mapAgentStatus(
        agent({
          scope: {
            status: "MAYBE", // drifted backend literal
            violations_today: 0,
            latest_violation: null,
          },
        }),
        NOW,
      ),
    ).toBe("degraded");
  });

  it("returns degraded when cost.status is an unknown string", () => {
    expect(
      mapAgentStatus(
        agent({
          cost: {
            status: "over_limit", // drifted backend literal
            usd_today: 10,
            tokens_today: 0,
            exceeded_today: 0,
          },
        }),
        NOW,
      ),
    ).toBe("degraded");
  });

  it("returns degraded for lowercase PASS (case-sensitive check)", () => {
    expect(
      mapAgentStatus(
        agent({
          scope: {
            status: "pass", // deliberate lowercase — catches case regression
            violations_today: 0,
            latest_violation: null,
          },
        }),
        NOW,
      ),
    ).toBe("degraded");
  });
});
