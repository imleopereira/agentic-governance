/**
 * F1 DrillPanel — runtime render + assert tests.
 *
 * v0.6.1 polish sprint: replaces the v0.6 source-grep test that
 * read DrillPanel.tsx off disk and regex-matched `role="complementary"`
 * / `<aside>` / no-focus-trap-call / no-Halt-button. Those claims are now
 * asserted against the rendered React tree.
 *
 * Includes an axe-core pass for the F1 a11y story the PRD wanted
 * proved at runtime.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { DrillPanel, TABS } from "./DrillPanel";
import type { AuditEvent, Posture, PostureAgent } from "@/lib/api";

// -- Mock the api surface; every panel in the drill reads from it. ----------
vi.mock("@/lib/api", async (orig) => {
  const actual = (await orig()) as Record<string, unknown>;
  return {
    ...actual,
    api: {
      posture: vi.fn(async () => samplePosture()),
      events: vi.fn(async () => [sampleEvent()]),
      costSessions: vi.fn(async () => []),
      getAgentPolicies: vi.fn(async () => ({
        agent_id: "agent-a",
        policies: [],
      })),
    },
  };
});

function samplePostureAgent(): PostureAgent {
  return {
    agent_id: "agent-a",
    event_count: 10,
    last_active: "2026-04-15T12:00:00.000Z",
    scope: { status: "PASS", violations_today: 0, latest_violation: null },
    cost: {
      status: "PASS",
      usd_today: 0.5,
      tokens_today: 1000,
      exceeded_today: 0,
    },
    gates: { status: "PASS", pending: 0 },
    audit: { status: "PASS", events_total: 10 },
  };
}

function samplePosture(): Posture {
  return {
    timestamp: "2026-04-15T12:00:00.000Z",
    agent_count: 1,
    agents: [samplePostureAgent()],
  };
}

function sampleEvent(): AuditEvent {
  return {
    event_id: "evt-1",
    session_id: "sess-1",
    agent_id: "agent-a",
    parent_event_id: null,
    kind: "tool.call",
    model: "gpt-4o",
    input_hash: null,
    output_hash: null,
    metadata: {},
    prev_hash: null,
    hmac_value: "deadbeef",
    chain_seq: 1,
    created_at: "2026-04-15T12:00:00.000Z",
  };
}

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("DrillPanel — TABS contract", () => {
  it("exports exactly 4 tabs (no Contracts)", () => {
    expect(TABS).toHaveLength(4);
    expect(TABS.map((t) => t.id)).toEqual([
      "trail",
      "scope",
      "budget",
      "sessions",
    ]);
  });
});

describe("DrillPanel — runtime a11y contracts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders an aside with role=complementary (non-modal drawer)", () => {
    render(
      <Wrapper>
        <DrillPanel agentId="agent-a" open onClose={() => {}} />
      </Wrapper>,
    );
    const aside = screen.getByRole("complementary");
    expect(aside.tagName.toLowerCase()).toBe("aside");
    // Must not announce itself as a modal dialog.
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders 4 tab triggers keyed to the TABS array", () => {
    render(
      <Wrapper>
        <DrillPanel agentId="agent-a" open onClose={() => {}} />
      </Wrapper>,
    );
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(4);
    const labels = tabs.map((t) => t.textContent?.trim());
    expect(labels).toEqual(["Trail", "Scope", "Budget", "Sessions"]);
  });

  it("does not render a disabled 'Halt (v0.6)' button", () => {
    render(
      <Wrapper>
        <DrillPanel agentId="agent-a" open onClose={() => {}} />
      </Wrapper>,
    );
    expect(screen.queryByText(/Halt \(v0\.6\)/)).toBeNull();
  });

  it("Escape key closes the panel (onClose called)", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Wrapper>
        <DrillPanel agentId="agent-a" open onClose={onClose} />
      </Wrapper>,
    );
    // Focus the aside so the keydown is handled on it.
    const aside = screen.getByRole("complementary");
    aside.focus();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("renders nothing when open=false", () => {
    const { container } = render(
      <Wrapper>
        <DrillPanel agentId="agent-a" open={false} onClose={() => {}} />
      </Wrapper>,
    );
    expect(container.firstChild).toBeNull();
  });

  // TODO(v0.6.2-ui): extend axe-core smoke to ScopePanel, EnforcementTrace,
  // and the other drill sub-panels (BudgetPanel, SessionsPanel, TrailPanel).
  // Wave 1 ships axe for 2 components (this + ComplianceHeaderPill) — the
  // "runtime a11y, not source-pinned" claim is established but not fully
  // covered. ScopePanel is highest-value next (most DOM, most pills, most
  // copy surface) followed by EnforcementTrace.
  it("passes axe-core accessibility smoke", async () => {
    const { container } = render(
      <Wrapper>
        <DrillPanel agentId="agent-a" open onClose={() => {}} />
      </Wrapper>,
    );
    // Wait for tabs to actually render before running axe.
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    const results = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });
});
