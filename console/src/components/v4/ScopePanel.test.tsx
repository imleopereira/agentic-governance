/**
 * F1 ScopePanel — runtime render + assert tests.
 *
 * v0.6.1 polish sprint: replaces the v0.6 source-grep test that
 * read `drill/ScopePanel.tsx` off disk and regex-matched for import
 * strings ("@/hooks/useAgentQueries"), the absence of `as { data: ... }`
 * casts, and the `<h3>Scope Policy</h3>` heading. The runtime test below
 * mounts the component (wired to the real `useAgentPolicy` hook via a
 * mocked api), and asserts what a keyboard/AT user actually sees.
 *
 * `derivePolicyView` unit coverage is kept — it's a pure function used
 * by the hook to flatten the backend response.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ScopePanel } from "./drill/ScopePanel";
import { derivePolicyView } from "@/hooks/useAgentQueries";
import type { AgentPoliciesResponse } from "@/lib/api";

vi.mock("@/lib/api", async (orig) => {
  const actual = (await orig()) as Record<string, unknown>;
  return {
    ...actual,
    api: {
      getAgentPolicies: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("derivePolicyView — pure kernel", () => {
  it("surfaces every string tool name from an AgentPoliciesResponse", () => {
    const mock: AgentPoliciesResponse = {
      agent_id: "agent-demo",
      policies: [
        {
          agent_id: "agent-demo",
          policy_type: "scope",
          policy: {
            allowed_tools: [
              "search_docs",
              "read_file",
              "write_file",
              "run_sql_readonly",
              "http_get",
            ],
          },
          updated_at: null,
        },
      ],
    };
    const view = derivePolicyView(mock);
    for (const tool of [
      "search_docs",
      "read_file",
      "write_file",
      "run_sql_readonly",
      "http_get",
    ]) {
      expect(view.allowed_tools).toContain(tool);
    }
  });
});

describe("ScopePanel — runtime render", () => {
  const getAgentPolicies = api.getAgentPolicies as unknown as ReturnType<
    typeof vi.fn
  >;

  beforeEach(() => {
    getAgentPolicies.mockReset();
  });

  it("renders a visible <h3>Scope Policy</h3> heading (WCAG 2.4.6)", async () => {
    getAgentPolicies.mockResolvedValueOnce({
      agent_id: "agent-a",
      policies: [],
    });
    render(
      <Wrapper>
        <ScopePanel agentId="agent-a" />
      </Wrapper>,
    );
    await waitFor(() => {
      const heading = screen.getByRole("heading", {
        level: 3,
        name: /Scope Policy/i,
      });
      expect(heading).toBeInTheDocument();
      expect(heading.tagName.toLowerCase()).toBe("h3");
    });
  });

  it("renders every string tool name in the DOM when policy is populated", async () => {
    getAgentPolicies.mockResolvedValueOnce({
      agent_id: "agent-a",
      policies: [
        {
          agent_id: "agent-a",
          policy_type: "scope",
          policy: {
            allowed_tools: [
              "search_docs",
              "read_file",
              "write_file",
              "run_sql_readonly",
              "http_get",
            ],
          },
          updated_at: null,
        },
      ],
    });
    render(
      <Wrapper>
        <ScopePanel agentId="agent-a" />
      </Wrapper>,
    );
    for (const tool of [
      "search_docs",
      "read_file",
      "write_file",
      "run_sql_readonly",
      "http_get",
    ]) {
      await waitFor(() => {
        expect(screen.getByText(tool)).toBeInTheDocument();
      });
    }
  });

  it("renders the EMPTY_STATES.scopePanel copy when policy is empty", async () => {
    getAgentPolicies.mockResolvedValueOnce({
      agent_id: "agent-a",
      policies: [],
    });
    render(
      <Wrapper>
        <ScopePanel agentId="agent-a" />
      </Wrapper>,
    );
    await waitFor(() => {
      expect(
        screen.getByText(/Allowlist enforced by SDK/i),
      ).toBeInTheDocument();
    });
  });

  it("renders a sanitized error message when the query fails", async () => {
    getAgentPolicies.mockRejectedValueOnce(new Error("network down"));
    render(
      <Wrapper>
        <ScopePanel agentId="agent-a" />
      </Wrapper>,
    );
    await waitFor(() => {
      expect(screen.getByText(/Failed to load policy/i)).toBeInTheDocument();
    });
    // The sanitized message must not leak a token / path / header.
    expect(screen.queryByText(/Bearer /i)).toBeNull();
  });

  it("drops non-string entries from the wire (defense-in-depth)", async () => {
    getAgentPolicies.mockResolvedValueOnce({
      agent_id: "agent-a",
      policies: [
        {
          agent_id: "agent-a",
          policy_type: "scope",
          policy: {
            allowed_tools: [
              "search_docs",
              { nefarious: true },
              42,
              "read_file",
            ],
          },
          updated_at: null,
        },
      ],
    });
    render(
      <Wrapper>
        <ScopePanel agentId="agent-a" />
      </Wrapper>,
    );
    await waitFor(() => {
      expect(screen.getByText("search_docs")).toBeInTheDocument();
    });
    expect(screen.getByText("read_file")).toBeInTheDocument();
    // Non-string entries must never reach the DOM.
    expect(screen.queryByText("42")).toBeNull();
    expect(screen.queryByText(/nefarious/i)).toBeNull();
  });
});
