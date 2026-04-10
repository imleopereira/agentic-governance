/**
 * Typed API client for the governance console backend.
 *
 * The backend runs at /api/* (proxied by Next.js rewrites in dev,
 * or configured via NEXT_PUBLIC_API_URL in production).
 */

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

async function get<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = new URL(`${BASE}${path}`, window.location.origin);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  }
  const token = typeof window !== "undefined"
    ? localStorage.getItem("governance_token") ?? ""
    : "";
  const res = await fetch(url.toString(), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) {
    throw new Error(`API ${path}: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

// ---------- Types ----------

export interface Agent {
  agent_id: string;
  event_count: number;
  last_active: string | null;
}

export interface AuditEvent {
  event_id: string;
  session_id: string;
  agent_id: string;
  parent_event_id: string | null;
  kind: string;
  input_hash: string | null;
  output_hash: string | null;
  metadata: Record<string, unknown>;
  prev_hash: string | null;
  chain_seq: number;
  created_at: string | null;
}

export interface VerifyResult {
  session_id: string;
  event_count: number;
  verified: boolean;
  events: Array<{ event_id: string; kind: string; verified: boolean }>;
  first_failure: string | null;
}

export interface CostAgent {
  agent_id: string;
  usd_used_today: number;
  tokens_used_today: number;
}

export interface CostSession {
  agent_id: string;
  session_id: string;
  usd_used: number;
  tokens_used: number;
  last_updated: string | null;
}

export interface GatePending {
  request_id: string;
  agent_id: string;
  kind: string;
  action_hash: string;
  created_at: string | null;
  expires_at: string | null;
}

export interface GateResolved {
  request_id: string;
  agent_id: string;
  kind: string;
  resolution: string;
  created_at: string | null;
  resolved_at: string | null;
}

export interface PostureAgent {
  agent_id: string;
  event_count: number;
  last_active: string | null;
  scope: { status: string; violations_today: number };
  cost: { status: string; usd_today: number; tokens_today: number; exceeded_today: number };
  gates: { status: string; pending: number };
  audit: { status: string; events_total: number };
}

export interface Posture {
  timestamp: string;
  agent_count: number;
  agents: PostureAgent[];
}

// ---------- API functions ----------

export const api = {
  health: () => get<{ ok: boolean; version: string }>("/api/health"),
  agents: (limit = 50) => get<Agent[]>("/api/agents", { limit: String(limit) }),
  events: (params?: { agent_id?: string; kind?: string; session_id?: string; limit?: number }) =>
    get<AuditEvent[]>("/api/events", {
      ...(params?.agent_id && { agent_id: params.agent_id }),
      ...(params?.kind && { kind: params.kind }),
      ...(params?.session_id && { session_id: params.session_id }),
      limit: String(params?.limit ?? 100),
    }),
  verifySession: (sessionId: string) =>
    get<VerifyResult>(`/api/session/${sessionId}/verify`),
  costAgents: () => get<CostAgent[]>("/api/cost/agents"),
  costSessions: (agentId?: string) =>
    get<CostSession[]>("/api/cost/sessions", agentId ? { agent_id: agentId } : {}),
  gatesPending: () => get<GatePending[]>("/api/gates/pending"),
  gatesRecent: (limit = 50) =>
    get<GateResolved[]>("/api/gates/recent", { limit: String(limit) }),
  posture: () => get<Posture>("/api/posture"),
};
