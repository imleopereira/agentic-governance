"use client";
import { useState, useMemo, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type PostureAgent } from "@/lib/api";
import { useEventStreamStore } from "@/lib/store";
import { TimeAgo } from "@/components/TimeAgo";
import { CardSkeleton } from "@/components/Skeleton";
import { useAuth } from "@/lib/auth";
import {
  Circle, AlertTriangle, X, Minus, Activity,
  DollarSign, ShieldAlert, Zap,
} from "lucide-react";

type AgentStatus = "live" | "idle" | "warn" | "dead";

function deriveStatus(agent: PostureAgent): AgentStatus {
  const hasIssue =
    agent.scope.status === "FAIL" || agent.cost.status === "FAIL" ||
    agent.scope.violations_today > 0 || agent.scope.status === "WARN" ||
    agent.cost.status === "WARN";
  if (!agent.last_active) return "dead";
  const age = (Date.now() - new Date(agent.last_active).getTime()) / 1000;
  if (hasIssue) return "warn";
  if (age > 300) return "dead";
  if (age < 60) return "live";
  return "idle";
}

const STATUS_CONFIG: Record<AgentStatus, { color: string; label: string; Icon: React.ElementType }> = {
  live: { color: "var(--status-live)", label: "LIVE", Icon: Circle },
  idle: { color: "var(--status-idle)", label: "IDLE", Icon: Minus },
  warn: { color: "var(--status-warn)", label: "WARN", Icon: AlertTriangle },
  dead: { color: "var(--status-dead)", label: "DEAD", Icon: X },
};

const DAILY_BUDGET = 10;

// ─── KPI Strip ────────────────────────────────────────────────────────────────
function KPIStrip({ agents }: { agents: PostureAgent[] }) {
  const sseEvents = useEventStreamStore((s) => s.events);
  const eventsPerMin = useMemo(() => {
    const cutoff = performance.now() - 60_000;
    return sseEvents.filter((e) => e._received_at > cutoff).length;
  }, [sseEvents]);
  const counts = useMemo(() => {
    const r = { live: 0, idle: 0, warn: 0, dead: 0, hitlBlocked: 0 };
    for (const a of agents) {
      r[deriveStatus(a)]++;
      if (a.gates.pending > 0) r.hitlBlocked++;
    }
    return r;
  }, [agents]);
  const kpis = [
    { label: "Agents",       value: agents.length,       icon: Activity,    color: "var(--text-secondary)" },
    { label: "Live",         value: counts.live,          icon: Circle,      color: "var(--status-live)" },
    { label: "Idle",         value: counts.idle,          icon: Minus,       color: "var(--status-idle)" },
    { label: "Warn",         value: counts.warn,          icon: AlertTriangle, color: "var(--status-warn)" },
    { label: "Dead",         value: counts.dead,          icon: X,           color: "var(--status-dead)" },
    { label: "HITL blocked", value: counts.hitlBlocked,  icon: ShieldAlert, color: "var(--status-hitl)" },
    { label: "Events/min",   value: eventsPerMin,         icon: Zap,         color: "var(--accent-light)" },
  ];
  return (
    <div role="region" aria-label="Agent fleet KPIs"
      style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(100px, 1fr))", gap: "0.75rem", marginBottom: "1.5rem" }}>
      {kpis.map((k) => {
        const Icon = k.icon;
        return (
          <div key={k.label} className="card" style={{ padding: "0.75rem 1rem", textAlign: "center" }}
            aria-label={`${k.label}: ${k.value}`}>
            <div style={{ display: "flex", justifyContent: "center", marginBottom: 4 }}>
              <Icon size={14} aria-hidden="true" style={{ color: k.color }} />
            </div>
            <p className="font-mono" style={{ fontSize: "1.375rem", fontWeight: 700, color: k.color, lineHeight: 1 }}
              aria-live="polite">{k.value}</p>
            <p style={{ fontSize: "0.6875rem", color: "var(--text-tertiary)", marginTop: 4 }}>{k.label}</p>
          </div>
        );
      })}
    </div>
  );
}

// ─── Budget bar & Agent Card ───────────────────────────────────────────────────
function BudgetBar({ usdToday }: { usdToday: number }) {
  const pct = Math.min(1, usdToday / DAILY_BUDGET);
  const color = pct > 0.9 ? "var(--danger)" : pct > 0.6 ? "var(--warn)" : "var(--success)";
  return (
    <div title={`$${usdToday.toFixed(4)} / $${DAILY_BUDGET} today`}
      aria-label={`Budget: ${Math.round(pct * 100)}% of daily limit`}
      style={{ height: 3, background: "rgba(255,255,255,0.06)", borderRadius: 2, overflow: "hidden" }}>
      <div style={{ height: "100%", width: `${pct * 100}%`, background: color, borderRadius: 2,
        transition: "width var(--transition-slow)" }} />
    </div>
  );
}

interface AgentCardProps { agent: PostureAgent; selected: boolean; onSelect: () => void }

function AgentCard({ agent, selected, onSelect }: AgentCardProps) {
  const status = deriveStatus(agent);
  const { color, label, Icon } = STATUS_CONFIG[status];
  return (
    <article role="article" aria-label={`${agent.agent_id}, status ${label}`}
      onClick={onSelect}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(); } }}
      tabIndex={0}
      style={{
        background: "var(--card)", border: `1px solid ${selected ? color : "var(--border)"}`,
        borderLeft: `3px solid ${color}`, borderRadius: "var(--radius-md)",
        padding: "0.875rem 1rem", cursor: "pointer",
        transition: "border-color var(--transition-slow), box-shadow var(--transition-slow), transform var(--transition-fast)",
        boxShadow: selected ? `0 0 0 1px ${color}22` : "var(--shadow-card)", outline: "none",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.transform = "translateY(-1px)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.transform = "translateY(0)"; }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8, marginBottom: 8 }}>
        <h3 className="font-mono" style={{ fontSize: "0.8125rem", fontWeight: 600, color: "var(--fg)",
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
          {agent.agent_id}
        </h3>
        <span aria-label={`Status: ${label}`}
          style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "2px 6px",
            borderRadius: "var(--radius-sm)", background: `${color}18`, border: `1px solid ${color}40`,
            fontSize: "0.6875rem", fontWeight: 600, color, flexShrink: 0 }}>
          <Icon size={10} aria-hidden="true" />{label}
        </span>
      </div>
      <BudgetBar usdToday={agent.cost.usd_today} />
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8,
        fontSize: "0.6875rem", color: "var(--text-tertiary)" }}>
        <span>{agent.audit.events_total.toLocaleString()} ev</span>
        <span>${agent.cost.usd_today.toFixed(4)}</span>
        {agent.last_active && <TimeAgo iso={agent.last_active} />}
      </div>
      {agent.scope.violations_today > 0 && (
        <p style={{ marginTop: 6, fontSize: "0.6875rem", color: "var(--danger)" }}>
          {agent.scope.violations_today} violation{agent.scope.violations_today !== 1 ? "s" : ""} today
        </p>
      )}
    </article>
  );
}

// ─── Detail Panel ─────────────────────────────────────────────────────────────
function MetricItem({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div>
      <p style={{ fontSize: "0.6875rem", color: "var(--text-tertiary)", marginBottom: 2 }}>{label}</p>
      <p style={{ fontSize: "0.9375rem", fontWeight: 600, color: warn ? "var(--warn)" : "var(--fg)",
        fontFamily: "var(--font-mono)" }}>{value}</p>
    </div>
  );
}

function DetailPanel({ agent, onClose }: { agent: PostureAgent; onClose: () => void }) {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const { color, label } = STATUS_CONFIG[deriveStatus(agent)];
  const [halting, setHalting] = useState(false);
  const [haltError, setHaltError] = useState<string | null>(null);
  const [haltDone, setHaltDone] = useState(false);
  const [confirmHalt, setConfirmHalt] = useState(false);
  const [haltReason, setHaltReason] = useState("");

  async function handleHalt() {
    setHalting(true); setHaltError(null);
    try {
      await api.haltAgent(agent.agent_id, haltReason || "Halted from console");
      setHaltDone(true);
      await queryClient.invalidateQueries({ queryKey: ["posture"] });
    } catch (err) {
      setHaltError(err instanceof Error ? err.message : "Halt failed");
    } finally { setHalting(false); setConfirmHalt(false); }
  }

  return (
    <section aria-label={`Detail for ${agent.agent_id}`}
      style={{ marginTop: "1rem", background: "var(--card)", border: `1px solid ${color}30`,
        borderLeft: `3px solid ${color}`, borderRadius: "var(--radius-md)",
        padding: "1rem 1.25rem", animation: "fade-in-up 0.2s ease-out" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "0.875rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="font-mono" style={{ fontWeight: 600, fontSize: "0.9375rem" }}>{agent.agent_id}</span>
          <span style={{ padding: "2px 7px", borderRadius: "var(--radius-sm)", background: `${color}18`,
            border: `1px solid ${color}40`, fontSize: "0.6875rem", fontWeight: 600, color }}>{label}</span>
        </div>
        <button onClick={onClose} aria-label="Close detail panel"
          style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-tertiary)", padding: 4 }}>
          <X size={16} aria-hidden="true" />
        </button>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: "0.75rem", marginBottom: "0.875rem" }}>
        <MetricItem label="Events total"      value={agent.audit.events_total.toLocaleString()} />
        <MetricItem label="Spend today"       value={`$${agent.cost.usd_today.toFixed(4)}`} />
        <MetricItem label="Tokens today"      value={agent.cost.tokens_today.toLocaleString()} />
        <MetricItem label="Scope violations"  value={String(agent.scope.violations_today)} warn={agent.scope.violations_today > 0} />
        <MetricItem label="Pending approvals" value={String(agent.gates.pending)} warn={agent.gates.pending > 0} />
        {agent.last_active && <MetricItem label="Last active" value={new Date(agent.last_active).toLocaleTimeString()} />}
      </div>
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
        <a href={`/events?agent_id=${encodeURIComponent(agent.agent_id)}`} className="btn-ghost"
          style={{ fontSize: "0.8125rem", padding: "0.375rem 0.75rem" }}>View Audit Trail</a>
        {user?.role === "admin" && !haltDone && (
          confirmHalt ? (
            <div role="alertdialog" aria-modal="true" aria-label="Confirm halt agent"
              style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <span style={{ fontSize: "0.8125rem", color: "var(--danger)" }}>
                Halt {agent.agent_id}? All enforcement gates will reject.
              </span>
              <span style={{ fontSize: "0.75rem", color: "var(--text-tertiary)" }}>
                The agent process keeps running but cannot pass any scope check, budget check, or contract enforcement. To terminate the process, use your orchestrator (K8s, systemd, etc.)
              </span>
              <input type="text" placeholder="Reason for halting..."
                value={haltReason} onChange={e => setHaltReason(e.target.value)}
                style={{ padding: "0.375rem 0.75rem", fontSize: "0.8125rem", background: "var(--bg)",
                  border: "1px solid var(--border)", color: "var(--text-primary)",
                  borderRadius: "var(--radius-sm)", width: "100%" }} />
              <div style={{ display: "flex", gap: 8 }}>
                <button onClick={handleHalt} disabled={halting} aria-label={`Confirm halt ${agent.agent_id}`}
                  style={{ padding: "0.375rem 0.75rem", fontSize: "0.8125rem", background: "var(--deny-bg)",
                    border: "1px solid var(--deny-border)", color: "var(--danger)",
                    borderRadius: "var(--radius-sm)", cursor: "pointer" }}>
                  {halting ? "Halting\u2026" : "Confirm Halt"}
                </button>
                <button onClick={() => setConfirmHalt(false)}
                  style={{ padding: "0.375rem 0.75rem", fontSize: "0.8125rem", background: "none",
                    border: "1px solid var(--border)", color: "var(--text-secondary)",
                    borderRadius: "var(--radius-sm)", cursor: "pointer" }}>Cancel</button>
              </div>
            </div>
          ) : (
            <button onClick={() => setConfirmHalt(true)} aria-label={`Halt agent ${agent.agent_id}`}
              style={{ padding: "0.375rem 0.75rem", fontSize: "0.8125rem", background: "var(--deny-bg)",
                border: "1px solid var(--deny-border)", color: "var(--danger)",
                borderRadius: "var(--radius-sm)", cursor: "pointer" }}>Halt Agent</button>
          )
        )}
        {haltDone && <span style={{ fontSize: "0.8125rem", color: "var(--success)" }}>Agent halted — all gates blocked</span>}
        {haltError && <span style={{ fontSize: "0.8125rem", color: "var(--danger)" }}>{haltError}</span>}
      </div>
    </section>
  );
}

// ─── Empty State ──────────────────────────────────────────────────────────────
function EmptyTopology() {
  const [copied, setCopied] = useState(false);
  const snippet = `from codeatelier_governance import GovernanceSDK

sdk = GovernanceSDK(database_url="postgresql://...")
agent = await sdk.register_agent("my-agent")`;
  return (
    <div style={{ textAlign: "center", padding: "4rem 2rem", maxWidth: 520, margin: "0 auto" }}>
      <div style={{ width: 56, height: 56, borderRadius: "50%",
        background: "rgba(130,40,245,0.1)", border: "1px solid rgba(130,40,245,0.2)",
        display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 1.25rem" }}>
        <Activity size={24} style={{ color: "var(--accent)" }} aria-hidden="true" />
      </div>
      <h2 style={{ fontSize: "1.125rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        No agents registered
      </h2>
      <p style={{ fontSize: "0.875rem", color: "var(--text-tertiary)", marginBottom: "1.5rem", lineHeight: 1.6 }}>
        Connect your first agent in 3 lines of Python. Once registered it will appear here with live status, budget usage, and audit events.
      </p>
      <div style={{ background: "rgba(0,0,0,0.3)", border: "1px solid var(--border)",
        borderRadius: "var(--radius-md)", padding: "1rem", textAlign: "left", position: "relative" }}>
        <pre className="font-mono" style={{ fontSize: "0.8125rem", color: "var(--fg)", margin: 0, whiteSpace: "pre-wrap" }}>
          {snippet}
        </pre>
        <button
          onClick={async () => { await navigator.clipboard.writeText(snippet).catch(() => null); setCopied(true); setTimeout(() => setCopied(false), 1500); }}
          aria-label="Copy code snippet"
          style={{ position: "absolute", top: 8, right: 8, background: "rgba(130,40,245,0.1)",
            border: "1px solid rgba(130,40,245,0.2)", color: copied ? "var(--accent-light)" : "var(--text-tertiary)",
            borderRadius: "var(--radius-sm)", padding: "4px 8px", fontSize: "0.6875rem", cursor: "pointer" }}>
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────
export default function TopologyPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["posture"],
    queryFn: api.posture,
    refetchInterval: 10_000,
  });

  const filtered = useMemo(() => {
    if (!data?.agents) return [];
    const q = search.trim().toLowerCase();
    return q ? data.agents.filter((a) => a.agent_id.toLowerCase().includes(q)) : data.agents;
  }, [data?.agents, search]);

  const selectedAgent = filtered.find((a) => a.agent_id === selectedId) ?? null;

  const handleGridKey = useCallback((e: React.KeyboardEvent) => {
    if (filtered.length === 0) return;
    const idx = selectedId ? filtered.findIndex((a) => a.agent_id === selectedId) : -1;
    if (e.key === "j" || e.key === "J") {
      e.preventDefault();
      setSelectedId(filtered[Math.min(filtered.length - 1, idx + 1)].agent_id);
    } else if (e.key === "k" || e.key === "K") {
      e.preventDefault();
      setSelectedId(filtered[Math.max(0, idx - 1)].agent_id);
    } else if (e.key === "Escape") {
      setSelectedId(null);
    } else if (e.key === "/") {
      e.preventDefault();
      document.getElementById("agent-search")?.focus();
    }
  }, [filtered, selectedId]);

  if (isLoading) {
    return (
      <div className="space-y-6" aria-busy="true">
        <h1 style={{ fontSize: "1.375rem", fontWeight: 700 }}>Agent Topology</h1>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: "0.75rem" }}>
          {Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} />)}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ textAlign: "center", padding: "5rem 2rem" }}>
        <div style={{ width: 48, height: 48, borderRadius: "var(--radius-md)",
          background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.25)",
          display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 1rem" }}>
          <X size={22} style={{ color: "var(--danger)" }} aria-hidden="true" />
        </div>
        <h2 style={{ fontSize: "1.125rem", marginBottom: "0.5rem", color: "var(--danger)" }}>
          Cannot reach governance API
        </h2>
        <p style={{ fontSize: "0.875rem", color: "var(--text-tertiary)" }}>
          Start the backend:{" "}
          <code className="font-mono" style={{ background: "rgba(130,40,245,0.1)",
            border: "1px solid rgba(130,40,245,0.2)", padding: "2px 6px", borderRadius: 3 }}>
            python -m codeatelier_governance.console
          </code>
        </p>
      </div>
    );
  }

  if (!data || data.agent_count === 0) {
    return <><h1 style={{ fontSize: "1.375rem", fontWeight: 700, marginBottom: "1rem" }}>Agent Topology</h1><EmptyTopology /></>;
  }

  return (
    <div onKeyDown={handleGridKey}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
        marginBottom: "1.25rem", gap: "1rem" }}>
        <h1 style={{ fontSize: "1.375rem", fontWeight: 700 }}>Agent Topology</h1>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <label htmlFor="agent-search" style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0,0,0,0)" }}>
            Search agents
          </label>
          <input id="agent-search" type="search" value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search agents… (/)"
            className="filter-input" style={{ width: 200 }}
            onKeyDown={(e) => { if (e.key === "Escape") setSearch(""); }}
            aria-label="Search agents by ID" />
        </div>
      </div>

      <KPIStrip agents={data.agents} />

      <div role="feed" aria-label={`${filtered.length} agents`}
        style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: "0.75rem" }}>
        {filtered.map((agent) => (
          <AgentCard key={agent.agent_id} agent={agent}
            selected={selectedId === agent.agent_id}
            onSelect={() => setSelectedId((id) => id === agent.agent_id ? null : agent.agent_id)} />
        ))}
      </div>

      {filtered.length === 0 && search && (
        <p style={{ textAlign: "center", padding: "2rem", color: "var(--text-tertiary)" }}>
          No agents matching &ldquo;{search}&rdquo;
        </p>
      )}

      {selectedAgent && (
        <DetailPanel agent={selectedAgent} onClose={() => setSelectedId(null)} />
      )}
    </div>
  );
}
