/**
 * F1 drill-panel loading-state contract (runtime render + assert).
 *
 * v0.6.1 polish sprint: replaces the v0.6 source-grep test that
 * read each of BudgetPanel / SessionsPanel / TrailPanel off disk and
 * regex-matched for `<Skeleton` + `aria-busy="true"` inside the
 * `isLoading` branch, plus an import-string check for Skeleton.
 *
 * The runtime test below mounts each panel against a hanging promise
 * (the mocked api call never resolves within the test window), then
 * asserts what the user actually sees:
 *
 *   - the panel announces itself as busy via `aria-busy="true"`,
 *   - the EmptyState's "No <foo>" title is NOT visible (loading and
 *     empty are different announcement categories — WCAG 4.1.3),
 *   - SessionsPanel renders the "Spend by session" title via the
 *     `SESSIONS_PANEL_TITLE` constant from `@/lib/empty-states`.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { BudgetPanel } from "./drill/BudgetPanel";
import { SessionsPanel } from "./drill/SessionsPanel";
import { TrailPanel } from "./drill/TrailPanel";
import { SESSIONS_PANEL_TITLE } from "@/lib/empty-states";

vi.mock("@/lib/api", async (orig) => {
  const actual = (await orig()) as Record<string, unknown>;
  return {
    ...actual,
    api: {
      posture: vi.fn(() => new Promise(() => {})),
      costSessions: vi.fn(() => new Promise(() => {})),
      events: vi.fn(() => new Promise(() => {})),
    },
  };
});

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe.each([
  ["BudgetPanel", BudgetPanel, /No activity today/i],
  ["SessionsPanel", SessionsPanel, /No sessions yet/i],
  ["TrailPanel", TrailPanel, /No events yet/i],
] as const)("%s — isLoading branch", (_name, PanelComp, emptyTitlePattern) => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders an aria-busy=true container while loading", () => {
    const { container } = render(
      <Wrapper>
        <PanelComp agentId="agent-a" />
      </Wrapper>,
    );
    // The outer container is flagged busy.
    const busy = container.querySelector('[aria-busy="true"]');
    expect(busy).not.toBeNull();
  });

  it("does NOT render the EmptyState title while loading", () => {
    render(
      <Wrapper>
        <PanelComp agentId="agent-a" />
      </Wrapper>,
    );
    // The empty-state path renders the panel's `emptyTitle`. Loading
    // and empty are different announcement categories and must not
    // overlap visually.
    expect(screen.queryByText(emptyTitlePattern)).toBeNull();
  });

  it("does not render role=status (EmptyState's semantic marker) while loading", () => {
    render(
      <Wrapper>
        <PanelComp agentId="agent-a" />
      </Wrapper>,
    );
    // EmptyState renders role=status; the loading skeleton must not.
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("SessionsPanel title constant", () => {
  it('resolves to "Spend by session" and is rendered by the panel', () => {
    expect(SESSIONS_PANEL_TITLE).toBe("Spend by session");
    render(
      <Wrapper>
        <SessionsPanel agentId="agent-a" />
      </Wrapper>,
    );
    // Shown inside a SectionLabel heading in the loading branch.
    expect(screen.getByText(SESSIONS_PANEL_TITLE)).toBeInTheDocument();
  });
});
