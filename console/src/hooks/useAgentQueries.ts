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
  type AuditEvent,
  type Posture,
  type PostureAgent,
} from "@/lib/api";

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
 * Scope + budget policy for a single agent.
 *
 * TODO: no `/api/policy/*` endpoints exist in `api.ts` yet. When Agent R /
 * backend ships them, wire them in here. For now this hook is a stable
 * shim so drill panels can depend on its signature.
 */
export function useAgentPolicy(
  id: string | null | undefined
): UseQueryResult<undefined, Error> {
  return useQuery<undefined, Error>({
    queryKey: ["agent", id, "policy"],
    enabled: false, // disabled until endpoints exist
    queryFn: async () => undefined,
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
