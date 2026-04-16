/**
 * DrillPanel — focus behaviour contract (runtime render + assert).
 *
 * v0.6.1 polish sprint: the v0.6 version of this test was a source-grep
 * over DrillPanel.tsx checking for `first.focus()` / `last.focus()`
 * tokens. It passed whether or not the component actually worked.
 *
 * The runtime test below mounts the panel and asserts the observable
 * behaviours the F1 removal was supposed to guarantee:
 *
 *   1. On open, focus lands inside the aside (first focusable control).
 *   2. Tab/Shift-Tab do NOT cycle inside the aside — the panel never
 *      intercepts Tab. Tab flow reaches a post-panel sentinel button.
 *   3. Clicking a tab trigger inside the panel does NOT steal focus
 *      from the tab control (this is the post-fix behaviour; Agent B
 *      removes `tab` from the focus-effect dep array in DrillPanel.tsx
 *      — see the coordination note in Agent C's brief).
 *   4. Escape closes the panel.
 *
 * NOTE: #3 depends on Agent B's DrillPanel.tsx fix (removing `tab`
 * from the focus-effect dependency array). If this test file is run
 * against the pre-fix tree, test #3 will fail with "focus moved to
 * the first focusable after tab click" — that's expected.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode, RefObject } from "react";
import { useRef } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { DrillPanel } from "./DrillPanel";
import type { AuditEvent, Posture, PostureAgent } from "@/lib/api";

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

function samplePosture(): Posture {
  const agent: PostureAgent = {
    agent_id: "agent-a",
    event_count: 5,
    last_active: "2026-04-15T12:00:00.000Z",
    scope: { status: "PASS", violations_today: 0, latest_violation: null },
    cost: {
      status: "PASS",
      usd_today: 0,
      tokens_today: 0,
      exceeded_today: 0,
    },
    gates: { status: "PASS", pending: 0 },
    audit: { status: "PASS", events_total: 5 },
  };
  return {
    timestamp: "2026-04-15T12:00:00.000Z",
    agent_count: 1,
    agents: [agent],
  };
}

function sampleEvent(): AuditEvent {
  return {
    event_id: "evt-1",
    session_id: "sess-1",
    agent_id: "agent-a",
    parent_event_id: null,
    kind: "tool.call",
    model: null,
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

function Harness({ onClose }: { onClose: () => void }) {
  const triggerRef = useRef<HTMLElement | null>(null);
  return (
    <>
      <button
        type="button"
        ref={triggerRef as RefObject<HTMLButtonElement | null>}
        data-testid="trigger"
      >
        open
      </button>
      <DrillPanel
        agentId="agent-a"
        open
        onClose={onClose}
        triggerRef={triggerRef}
      />
      {/* Post-panel sentinel proves Tab flows out of the aside.
          If a focus trap were reintroduced, Tab would cycle back
          to the Close button instead of reaching this element. */}
      <button type="button" data-testid="after-panel">
        after
      </button>
    </>
  );
}

describe("DrillPanel — focus behaviour (runtime)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("moves focus into the aside on open", async () => {
    render(
      <Wrapper>
        <Harness onClose={() => {}} />
      </Wrapper>,
    );
    // The effect picks the first focusable inside the aside. With the
    // mocked queries resolving async, the Close button is guaranteed to
    // be the first match (it renders synchronously inside the aside).
    const closeBtn = screen.getByRole("button", { name: /Close drill panel/i });
    // Allow React effects to flush.
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.activeElement).toBe(closeBtn);
  });

  it("Escape closes the panel", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Wrapper>
        <Harness onClose={onClose} />
      </Wrapper>,
    );
    screen.getByRole("complementary").focus();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("Tab is NOT intercepted inside the aside (no focus trap)", async () => {
    const user = userEvent.setup();
    render(
      <Wrapper>
        <Harness onClose={() => {}} />
      </Wrapper>,
    );
    // Move focus to the last tab trigger — one Tab press must land
    // outside the aside (the post-panel sentinel), proving nothing is
    // clamping focus back to the top of the aside.
    const tabs = screen.getAllByRole("tab");
    const lastTab = tabs[tabs.length - 1];
    lastTab.focus();
    expect(document.activeElement).toBe(lastTab);
    // Tab once: must leave the aside. The tabpanel (tabIndex undefined)
    // is not focusable, so the next focusable is `after-panel`.
    await user.tab();
    // Either the tabpanel or the sentinel — assert we're NOT back at
    // the Close button (which would indicate a trap cycled focus).
    const closeBtn = screen.getByRole("button", { name: /Close drill panel/i });
    expect(document.activeElement).not.toBe(closeBtn);
    // And we MUST eventually reach the sentinel with at most one more tab.
    if (document.activeElement?.getAttribute("data-testid") !== "after-panel") {
      await user.tab();
    }
    expect(document.activeElement).toBe(
      screen.getByTestId("after-panel"),
    );
  });

  it("clicking a tab does NOT steal focus from the tab control (requires Agent B fix)", async () => {
    const user = userEvent.setup();
    render(
      <Wrapper>
        <Harness onClose={() => {}} />
      </Wrapper>,
    );
    // Flush the initial focus effect.
    await act(async () => {
      await Promise.resolve();
    });
    const budgetTab = screen.getByRole("tab", { name: "Budget" });
    await user.click(budgetTab);
    // Post-fix: the focus effect only re-runs on [open, triggerRef],
    // so clicking a tab does NOT re-fire `first.focus()` on the aside
    // Close button. Focus stays on the clicked tab.
    expect(document.activeElement).toBe(budgetTab);
  });
});
