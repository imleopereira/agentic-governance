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

/** Module-health literal emitted by the backend posture endpoint.
 *  Source: `src/codeatelier_governance/console/app.py` (governance_posture),
 *  lines ~1766-1812 of v0.6. Values are EXACTLY these three strings —
 *  narrowing the wire shape here lets every consumer (statusMap, posture
 *  badge, compliance pill) avoid `as string` casts and receive exhaustive
 *  switch coverage. Unknown literal values from a drifted backend are
 *  a programmer error and are handled at the renderer layer
 *  (see `mapAgentStatus` — `_unknownStatusWarned` fail-loud). */
export type PostureStatus = "PASS" | "WARN" | "FAIL";

export interface PostureAgent {
  agent_id: string;
  event_count: number;
  last_active: string | null;
  scope: {
    status: PostureStatus;
    violations_today: number;
    latest_violation: { tool: string; created_at: string | null } | null;
  };
  cost: {
    status: PostureStatus;
    usd_today: number;
    tokens_today: number;
    exceeded_today: number;
  };
  gates: { status: PostureStatus; pending: number };
  audit: { status: PostureStatus; events_total: number };
}

export interface Posture {
  timestamp: string;
  agent_count: number;
  agents: PostureAgent[];
}

/** Mirrors backend `PolicyRow` Pydantic model (responses.py).
 *
 *  DA Wave 4 blocker fix (F1): the four scope list fields are now typed
 *  top-level attributes on the backend model. `policy` remains strictly
 *  scalar-only (`MetadataValue`). Readers SHOULD prefer the top-level
 *  `allowed_tools` etc., and fall back to parsing `policy.allowed_tools`
 *  only for legacy v0.5.x rows that pre-date the split. See
 *  `derivePolicyView` in `useAgentQueries.ts` for the canonical reader. */
export interface PolicyRow {
  agent_id: string;
  policy_type: string;
  policy: Record<string, unknown>;
  allowed_tools?: string[] | null;
  hidden_tools?: string[] | null;
  allowed_apis?: string[] | null;
  allowed_models?: string[] | null;
  updated_at: string | null;
}

/** Mirrors backend `AgentPoliciesResponse` — GET /api/policies/{agent_id}. */
export interface AgentPoliciesResponse {
  agent_id: string;
  policies: PolicyRow[];
}

// ---------- F4 Compliance surface types ----------

export interface ComplianceReportView {
  report_id: string;
  generated_at: string;
  chain_integrity_status: "verified" | "unverified" | "degraded" | "halted";
  chain_verified_from_seq: number | null;
  chain_verified_to_seq: number | null;
  coverage_pct: number | null;
  coverage_pct_reason:
    | "no_scope_policies_registered"
    | "registry_disabled"
    | "ok"
    | null;
  coverage_caveat: string | null;
  total_events_audited: number;
  total_agents: number;
}

export interface VerifyChainResponse {
  chain_integrity_status: "verified" | "unverified" | "degraded" | "halted";
  from_seq: number | null;
  to_seq: number | null;
  verified_count: number;
  failed_count: number;
  unresolved_fingerprints: string[];
  verified_at_utc: string;
}

/** Request body for POST /api/compliance/export — all fields optional.
 *  When omitted, the backend defaults the window to the last 7 days. */
export interface ComplianceExportRequest {
  window_start?: string;
  window_end?: string;
  tenant_id?: string | null;
}

/** Response shape for POST /api/compliance/export.
 *
 *  The bundle is self-verifying: ``bundle_hash`` is sha256 over the
 *  canonical body without ``bundle_hash`` / ``bundle_signature``, and
 *  ``bundle_signature.signature`` is HMAC-SHA256 over the canonical
 *  body without ``bundle_signature``. Archive the whole JSON for
 *  regulator submission; the active ``bundle_signature.key_fingerprint``
 *  proves which audit secret was in effect when the bundle was issued. */
export interface ComplianceBundleResponse {
  bundle_version: "1.0";
  generated_at: string;
  tenant_id: string | null;
  window: { start: string; end: string };
  report: ComplianceReportView;
  verify_chain: VerifyChainResponse | null;
  chain_verification_error: string | null;
  rotation_status: {
    active_fingerprint: string;
    known_fingerprints_in_window: string[];
  };
  event_count: number;
  bundle_hash: string;
  bundle_signature: {
    algorithm: "HMAC-SHA256";
    key_fingerprint: string;
    signature: string;
  };
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
   *  v0.6 F2.5: backend route renamed from `/kill` → `/halt`. The old `/kill`
   *  route still exists as a deprecated alias (Deprecation + Sunset headers)
   *  for one release so v0.5.x callers keep working while they upgrade. */
  haltAgent: (agentId: string, reason: string) =>
    post<{ ok: boolean; agent_id: string }>(`/api/agents/${agentId}/halt`, { reason }),

  /** Fetch a single audit event by ID, for SSE-stream hydration (the NOTIFY
   *  trigger payload omits `model`/`metadata`/`hmac_value`/`prev_hash` to keep
   *  the WAL payload small). Used by the event-stream store when the consumer
   *  first opens an event whose `_needs_hydration` flag is true. */
  getAuditEvent: (eventId: string) =>
    get<AuditEvent>(`/api/events/${encodeURIComponent(eventId)}`),

  /** F3 typed endpoint: scope + budget policy rows for one agent. */
  getAgentPolicies: (agentId: string) =>
    get<AgentPoliciesResponse>(
      `/api/policies/${encodeURIComponent(agentId)}`
    ),

  /** F4: Article 12 evidence summary view. */
  getComplianceReport: () =>
    get<ComplianceReportView>("/api/compliance/report"),

  /** F4: on-demand HMAC chain re-verification.
   *
   *  ``from_seq`` / ``to_seq`` are optional — when omitted the backend
   *  verifies the last 1000 events (DA blocker: verify_chain is O(n)). */
  verifyChain: (args: { from_seq?: number; to_seq?: number }) => {
    const params: Record<string, string> = {};
    if (args.from_seq !== undefined) params.from_seq = String(args.from_seq);
    if (args.to_seq !== undefined) params.to_seq = String(args.to_seq);
    const qs = new URLSearchParams(params).toString();
    return post<VerifyChainResponse>(
      `/api/compliance/verify-chain${qs ? `?${qs}` : ""}`,
    );
  },

  /** F4 polish (v0.6.1): download a signed Article 12 evidence bundle.
   *
   *  Packages the current ``compliance_report`` + ``verify_chain``
   *  outputs into a self-verifying JSON bundle with an HMAC-SHA256
   *  signature under the active ``AUDIT_SECRET``. The bundle is
   *  append-only evidence — not a real-time integrity check.
   *
   *  Rate limited by the same 1 req/60 s/user F4 bucket as the other
   *  compliance endpoints — the Download button should debounce and
   *  surface the 429 ``Retry-After`` inline on failure. */
  exportComplianceBundle: (body: ComplianceExportRequest = {}) =>
    post<ComplianceBundleResponse>("/api/compliance/export", body),
};
