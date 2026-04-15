/**
 * Typed API client for the governance console backend.
 *
 * Auth: Uses httpOnly session cookies (set by /api/auth/login).
 * Falls back to Bearer token from localStorage for legacy compat.
 */

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

async function get<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = new URL(`${BASE}${path}`, window.location.origin);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  }
  const token =
    typeof window !== "undefined"
      ? localStorage.getItem("governance_token") ?? ""
      : "";
  const res = await fetch(url.toString(), {
    credentials: "include",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (res.status === 401) {
    localStorage.removeItem("governance_token");
    // Try to extract detail from JSON response
    const detail401 = await res.json().then((j) => j.detail).catch(() => null);
    throw new Error(detail401 || "Authentication required");
  }
  if (!res.ok) {
    const detail = await res.json().then((j) => j.detail).catch(() => null);
    throw new Error(detail || `API ${path}: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const url = new URL(`${BASE}${path}`, window.location.origin);
  const token =
    typeof window !== "undefined"
      ? localStorage.getItem("governance_token") ?? ""
      : "";
  const res = await fetch(url.toString(), {
    method: "POST",
    credentials: "include",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    // FastAPI returns {"detail": "..."} on errors — prefer that over raw status
    const detail = await res.json().then((j) => j.detail).catch(() => null);
    if (detail) {
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
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
  model?: string | null;
  input_hash: string | null;
  output_hash: string | null;
  metadata: Record<string, unknown>;
  prev_hash: string | null;
  hmac_value: string | null;
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

export interface CostModel {
  agent_id: string;
  model: string;
  usd_used_today: number;
  tokens_used_today: number;
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
  rationale?: string | null;
  resolved_by?: string | null;
}

export interface PostureAgent {
  agent_id: string;
  event_count: number;
  last_active: string | null;
  scope: {
    status: string;
    violations_today: number;
    latest_violation: { tool: string; created_at: string | null } | null;
  };
  cost: {
    status: string;
    usd_today: number;
    tokens_today: number;
    exceeded_today: number;
  };
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
  agents: (limit = 50) =>
    get<Agent[]>("/api/agents", { limit: String(limit) }),
  events: (params?: {
    agent_id?: string;
    kind?: string;
    session_id?: string;
    limit?: number;
    offset?: number;
  }) =>
    get<AuditEvent[]>("/api/events", {
      ...(params?.agent_id && { agent_id: params.agent_id }),
      ...(params?.kind && { kind: params.kind }),
      ...(params?.session_id && { session_id: params.session_id }),
      limit: String(params?.limit ?? 100),
      offset: String(params?.offset ?? 0),
    }),
  verifySession: (sessionId: string) =>
    get<VerifyResult>(`/api/session/${sessionId}/verify`),
  costAgents: () => get<CostAgent[]>("/api/cost/agents"),
  costSessions: (agentId?: string) =>
    get<CostSession[]>(
      "/api/cost/sessions",
      agentId ? { agent_id: agentId } : {}
    ),
  costModels: () => get<CostModel[]>("/api/cost/models"),
  gatesPending: () => get<GatePending[]>("/api/gates/pending"),
  gatesRecent: (limit = 50) =>
    get<GateResolved[]>("/api/gates/recent", { limit: String(limit) }),
  posture: () => get<Posture>("/api/posture"),
  grantGate: (requestId: string) =>
    post<{ ok: boolean; request_id: string; resolution: string }>(
      `/api/gates/${requestId}/grant`
    ),
  /** Deny a gate with a required rationale (stored in audit trail). */
  denyGate: (requestId: string, rationale?: string) =>
    post<{ ok: boolean; request_id: string; resolution: string }>(
      `/api/gates/${requestId}/deny`,
      rationale ? { rationale } : undefined
    ),
  /** Halt agent — blocks all enforcement gates. The agent process keeps running
   *  but cannot pass any scope check, budget check, or contract enforcement.
   *  Process termination is the host application's responsibility. Admin only.
   *
   *  Note: backend route is still `/kill` in v0.6; the full rename to `/halt`
   *  lands in F2.5. This call site is named `haltAgent` for the product-level
   *  vocabulary while hitting the current wire URL. */
  haltAgent: (agentId: string, reason: string) =>
    post<{ ok: boolean; agent_id: string }>(`/api/agents/${agentId}/kill`, { reason }),

  /** Fetch a single audit event by ID, for SSE-stream hydration (the NOTIFY
   *  trigger payload omits `model`/`metadata`/`hmac_value`/`prev_hash` to keep
   *  the WAL payload small). Used by the event-stream store when the consumer
   *  first opens an event whose `_needs_hydration` flag is true. */
  getAuditEvent: (eventId: string) =>
    get<AuditEvent>(`/api/events/${encodeURIComponent(eventId)}`),
};
