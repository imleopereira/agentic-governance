/**
 * Consolidated TanStack Query hooks for the v4 drill panels.
 *
 * All hooks use stable query keys so they dedupe across panels.
 * staleTime / gcTime / retry come from the default options in
 * `@/lib/queryClient`; callers do not need to override.
 *
 * TODO(Security H2): when multi-tenant ships, query keys MUST include
 * the active tenant/user id so that a re-login as a different user on
 * the same browser tab cannot read another tenant's cached rows. The
 * SDK is single-tenant today and changing keys now would invalidate
 * every existing cache for zero security benefit, so this is a
 * comment-only marker. Pair with `queryClient.clear()` in auth.logout
 * (already wired — see `@/lib/auth`) which is the interim defense.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import {
  api,
  type AgentPoliciesResponse,
  type AuditEvent,
  type Posture,
  type PostureAgent,
} from "../lib/api";

/** Flat view of scope policy the ScopePanel actually renders. Derived
 *  client-side from the F3 `AgentPoliciesResponse` shape. All fields are
 *  typed as `readonly string[]` after sanitization — any non-string entry
 *  from the wire is dropped so nothing that bypasses the Pydantic
 *  `extra="forbid"` guard on the backend can reach the DOM. */
export interface ScopePolicyView {
  readonly allowed_tools: readonly string[];
  readonly hidden_tools: readonly string[];
  readonly allowed_apis: readonly string[];
  readonly allowed_models: readonly string[];
}

export function coerceStringList(raw: unknown): readonly string[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((v): v is string => typeof v === "string");
}

/** Collapse an `AgentPoliciesResponse` into the flat shape ScopePanel
 *  renders. Exported for unit tests — see `ScopePanel.test.ts`.
 *
 *  DA Wave 4 fix (F1): prefer the typed top-level row fields
 *  (`row.allowed_tools`, etc.) that the v0.6 backend now populates.
 *  Falls back to parsing `row.policy.<field>` for legacy v0.5.x rows
 *  that pre-date the split. `coerceStringList` is kept as a defensive
 *  narrow in both paths — never trust the wire, drop non-strings
 *  fail-closed so nothing that bypasses the Pydantic `extra="forbid"`
 *  guard can reach the DOM. */
export function derivePolicyView(
  resp: AgentPoliciesResponse
): ScopePolicyView {
  const scopeRow = resp.policies.find((p) => p.policy_type === "scope");
  const scope = scopeRow?.policy ?? {};
  const pick = (
    typed: unknown,
    legacyKey: "allowed_tools" | "hidden_tools" | "allowed_apis" | "allowed_models",
  ): readonly string[] => {
    if (Array.isArray(typed)) return coerceStringList(typed);
    return coerceStringList((scope as Record<string, unknown>)[legacyKey]);
  };
  return {
    allowed_tools: pick(scopeRow?.allowed_tools, "allowed_tools"),
    hidden_tools: pick(scopeRow?.hidden_tools, "hidden_tools"),
    allowed_apis: pick(scopeRow?.allowed_apis, "allowed_apis"),
    allowed_models: pick(scopeRow?.allowed_models, "allowed_models"),
  };
}

/** Single agent, filtered client-side out of /api/posture. */
export function useAgent(
  id: string | null | undefined
): UseQueryResult<PostureAgent | undefined, Error> {
  return useQuery<PostureAgent | undefined, Error>({
    queryKey: ["agent", id],
    enabled: Boolean(id),
    queryFn: async () => {
      const posture: Posture = await api.posture();
      return posture.agents.find((a) => a.agent_id === id);
    },
  });
}

/** Audit trail for a single agent, most-recent first from /api/events. */
export function useAgentTrail(
  id: string | null | undefined,
  limit = 100
): UseQueryResult<AuditEvent[], Error> {
  return useQuery<AuditEvent[], Error>({
    queryKey: ["agent", id, "trail", limit],
    enabled: Boolean(id),
    queryFn: () => api.events({ agent_id: id as string, limit }),
  });
}

/**
 * Scope policy for a single agent — wired to F3's typed endpoint
 * `GET /api/policies/{agent_id}`. Backend response goes through
 * `derivePolicyView` so the panel only sees a flat, sanitized
 * `ScopePolicyView` (non-string array entries are dropped client-side,
 * fail-closed — never trust the Pydantic `extra="forbid"` guard alone).
 */
export function useAgentPolicy(
  id: string | null | undefined
): UseQueryResult<ScopePolicyView, Error> {
  return useQuery<ScopePolicyView, Error>({
    queryKey: ["agent", id, "policy"],
    enabled: Boolean(id),
    queryFn: async () => {
      const resp = await api.getAgentPolicies(id as string);
      return derivePolicyView(resp);
    },
  });
}

/**
 * Per-session cost rows for an agent, sourced from /api/cost/sessions.
 * Used by the drill-over's "Sessions" tab as a temporary stand-in until
 * a dedicated sessions endpoint exists.
 */
export function useAgentSessions(
  id: string | null | undefined
): UseQueryResult<Awaited<ReturnType<typeof api.costSessions>>, Error> {
  return useQuery({
    queryKey: ["agent", id, "sessions"],
    enabled: Boolean(id),
    queryFn: () => api.costSessions(id as string),
  });
}
