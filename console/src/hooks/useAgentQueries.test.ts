/**
 * F1 Console Honesty Pass — scope policy derivation.
 *
 * `useAgentPolicy` wraps TanStack Query so it can't be called outside a
 * React tree without a QueryClient. The derivation helper
 * `derivePolicyView` is the pure function the hook delegates to, and
 * it is what ScopePanel effectively renders. Tests the full contract
 * at the layer we actually care about: nothing that isn't a string
 * in `allowed_tools` can reach the DOM.
 */
import { describe, it, expect } from "vitest";
import { coerceStringList, derivePolicyView } from "./useAgentQueries";
import type { AgentPoliciesResponse } from "../lib/api";

function resp(
  scopePolicy: Record<string, unknown>,
): AgentPoliciesResponse {
  return {
    agent_id: "demo",
    policies: [
      {
        agent_id: "demo",
        policy_type: "scope",
        policy: scopePolicy,
        updated_at: null,
      },
    ],
  };
}

describe("derivePolicyView", () => {
  it("returns empty arrays for a missing scope row", () => {
    const view = derivePolicyView({ agent_id: "demo", policies: [] });
    expect(view.allowed_tools).toEqual([]);
    expect(view.hidden_tools).toEqual([]);
    expect(view.allowed_apis).toEqual([]);
    expect(view.allowed_models).toEqual([]);
  });

  it("renders every allowed_tools string", () => {
    const view = derivePolicyView(
      resp({ allowed_tools: ["search", "read", "write_safe"] }),
    );
    expect(view.allowed_tools).toEqual(["search", "read", "write_safe"]);
  });

  it("drops non-string entries from allowed_tools (XSS fail-closed)", () => {
    const view = derivePolicyView(
      resp({
        allowed_tools: [
          "search",
          { tool: "<img src=x onerror=1>", db_host: "10.0.0.5" },
          42,
          null,
          "read",
        ],
      }),
    );
    expect(view.allowed_tools).toEqual(["search", "read"]);
  });

  it("handles scalar policy values (backend Pydantic type drift)", () => {
    // If a drifted backend returns a scalar in place of a list, we
    // must NOT throw — we must return an empty array.
    const view = derivePolicyView(
      resp({ allowed_tools: "search", allowed_apis: null }),
    );
    expect(view.allowed_tools).toEqual([]);
    expect(view.allowed_apis).toEqual([]);
  });

  // --- DA Wave 4 edge cases ----------------------------------------------

  it("coerceStringList drops non-string entries", () => {
    expect(coerceStringList([1, "tool_a", null, "tool_b"])).toEqual([
      "tool_a",
      "tool_b",
    ]);
  });

  it("coerceStringList returns [] for an empty array", () => {
    expect(coerceStringList([])).toEqual([]);
  });

  it("derivePolicyView reads typed top-level row.allowed_tools first", () => {
    // DA Wave 4 F1: new backend populates the list fields at the row
    // level; the scope dict has ONLY scalars.
    const view = derivePolicyView({
      agent_id: "demo",
      policies: [
        {
          agent_id: "demo",
          policy_type: "scope",
          policy: {},
          allowed_tools: ["a", "b"],
          hidden_tools: null,
          allowed_apis: null,
          allowed_models: null,
          updated_at: null,
        },
      ],
    });
    expect(view.allowed_tools).toEqual(["a", "b"]);
  });

  it("derivePolicyView returns empty tools when scope policy is missing", () => {
    const view = derivePolicyView({
      agent_id: "demo",
      policies: [
        {
          agent_id: "demo",
          policy_type: "budget",
          policy: { usd_cap: 10 },
          updated_at: null,
        },
      ],
    });
    expect(view.allowed_tools).toEqual([]);
    expect(view.hidden_tools).toEqual([]);
  });

  it("derivePolicyView reads legacy policy.allowed_tools when row field absent", () => {
    // Legacy v0.5.x path — the row field is undefined, so the reader
    // falls back to the scope dict.
    const view = derivePolicyView(resp({ allowed_tools: ["legacy"] }));
    expect(view.allowed_tools).toEqual(["legacy"]);
  });

  it("narrows all four list fields uniformly", () => {
    const view = derivePolicyView(
      resp({
        allowed_tools: ["a"],
        hidden_tools: ["b"],
        allowed_apis: ["c"],
        allowed_models: ["d"],
      }),
    );
    expect(view).toEqual({
      allowed_tools: ["a"],
      hidden_tools: ["b"],
      allowed_apis: ["c"],
      allowed_models: ["d"],
    });
  });
});
