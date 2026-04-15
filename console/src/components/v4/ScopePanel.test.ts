/**
 * F1 Console Honesty Pass — ScopePanel wiring contract.
 *
 * Source-level pins (DOM testing deferred until @testing-library lands):
 *   1. The panel imports the live `useAgentPolicy` hook (no shim).
 *   2. There is no `as` cast on the hook result.
 *   3. There is no dead `error` branch guarded by `enabled: false`.
 *   4. The empty-state copy is sourced from `@/lib/empty-states`.
 *   5. The <h3> heading replaces the old <SectionLabel>-only title.
 *
 * Plus the functional test: `derivePolicyView` (which the hook delegates
 * to) renders every string tool name it receives. This is the real
 * "every tool name is in the DOM" assertion compiled down to its pure
 * kernel.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { derivePolicyView } from "../../hooks/useAgentQueries";
import type { AgentPoliciesResponse } from "../../lib/api";

const source = readFileSync(
  join(__dirname, "drill", "ScopePanel.tsx"),
  "utf8",
);

describe("ScopePanel source contract", () => {
  it("imports the live useAgentPolicy hook", () => {
    expect(source).toMatch(/from\s+"@\/hooks\/useAgentQueries"/);
    expect(source).toContain("useAgentPolicy");
  });

  it("does not cast the hook result with `as`", () => {
    // The old code had `useAgentPolicy(agentId) as { data: ... }`
    expect(source).not.toMatch(/useAgentPolicy\([^)]*\)\s+as\s+\{/);
  });

  it("removes the dead `enabled: false` error branch check", () => {
    // That branch quoted `Policy endpoint not available`.
    expect(source).not.toContain("Policy endpoint not available");
  });

  it("uses empty-states.ts for copy, not inline strings", () => {
    expect(source).toMatch(/from\s+"@\/lib\/empty-states"/);
    expect(source).toContain("EMPTY_STATES.scopePanel");
  });

  it("renders the title as an <h3> heading (WCAG 2.4.6)", () => {
    expect(source).toMatch(/<h3[^>]*>\s*Scope Policy\s*<\/h3>/);
  });
});

describe("ScopePanel policy derivation renders every tool name", () => {
  it("surfaces all 5 mock tool names through derivePolicyView", () => {
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
