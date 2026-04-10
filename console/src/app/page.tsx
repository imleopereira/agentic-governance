"use client";

import { useQuery } from "@tanstack/react-query";
import { api, type PostureAgent } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { CardSkeleton } from "@/components/Skeleton";
import { LiveBadge } from "@/components/LiveBadge";
import { TimeAgo } from "@/components/TimeAgo";
import { GettingStarted } from "@/components/GettingStarted";

function PostureCard({ agent }: { agent: PostureAgent }) {
  return (
    <a
      href={`/events?agent_id=${encodeURIComponent(agent.agent_id)}`}
      className="block rounded-lg border border-[var(--border)] bg-[var(--card)] p-4 space-y-3 hover:border-[var(--accent)]/40 transition-colors"
    >
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-sm truncate">{agent.agent_id}</h3>
        <span className="text-xs text-[var(--muted)]">
          {agent.event_count.toLocaleString()} events
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs" data-tour="status-badge">
        <div className="flex items-center justify-between p-2 rounded bg-black/30">
          <span>Scope</span>
          <StatusBadge status={agent.scope.status} />
        </div>
        <div className="flex items-center justify-between p-2 rounded bg-black/30">
          <span>Cost</span>
          <StatusBadge status={agent.cost.status} />
        </div>
        <div className="flex items-center justify-between p-2 rounded bg-black/30">
          <span>Gates</span>
          <StatusBadge status={agent.gates.status} />
        </div>
        <div className="flex items-center justify-between p-2 rounded bg-black/30">
          <span>Audit</span>
          <StatusBadge status={agent.audit.status} />
        </div>
      </div>

      {agent.cost.usd_today > 0 && (
        <p className="text-xs text-[var(--muted)]">
          Today: ${agent.cost.usd_today.toFixed(4)} /{" "}
          {agent.cost.tokens_today.toLocaleString()} tokens
        </p>
      )}
      {agent.scope.violations_today > 0 && (
        <p className="text-xs text-[var(--danger)]">
          {agent.scope.violations_today} scope violation(s) today
        </p>
      )}
      {agent.gates.pending > 0 && (
        <p className="text-xs text-[var(--warn)]">
          {agent.gates.pending} approval(s) pending
        </p>
      )}
      {agent.last_active && (
        <p className="text-xs text-[var(--muted)]">
          Last active: <TimeAgo iso={agent.last_active} />
        </p>
      )}
    </a>
  );
}

function StatsBar({ agents }: { agents: PostureAgent[] }) {
  const totalEvents = agents.reduce((s, a) => s + a.event_count, 0);
  const totalSpend = agents.reduce((s, a) => s + a.cost.usd_today, 0);
  const pendingGates = agents.reduce((s, a) => s + a.gates.pending, 0);

  return (
    <div className="grid grid-cols-3 gap-4">
      <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-center">
        <p className="text-2xl font-bold">{totalEvents.toLocaleString()}</p>
        <p className="text-xs text-[var(--muted)]">Total events</p>
      </div>
      <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-center">
        <p className="text-2xl font-bold">${totalSpend.toFixed(4)}</p>
        <p className="text-xs text-[var(--muted)]">Spend today</p>
      </div>
      <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-center">
        <p className={`text-2xl font-bold ${pendingGates > 0 ? "text-[var(--warn)]" : ""}`}>
          {pendingGates}
        </p>
        <p className="text-xs text-[var(--muted)]">Pending approvals</p>
      </div>
    </div>
  );
}

export default function PosturePage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["posture"],
    queryFn: api.posture,
  });

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">Governance Posture</h1>
            <p className="text-sm text-[var(--muted)]">Loading...</p>
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <CardSkeleton key={i} />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-center py-20">
        <h2 className="text-xl mb-2 text-[var(--danger)]">Connection error</h2>
        <p className="text-sm text-[var(--muted)] mb-4">
          Could not reach the governance API at /api/posture.
        </p>
        <p className="text-xs text-[var(--muted)]">
          Make sure the backend is running:{" "}
          <code className="bg-black/30 px-1 rounded">
            python -m codeatelier_governance.console
          </code>
        </p>
      </div>
    );
  }

  if (!data || data.agent_count === 0) {
    return <GettingStarted />;
  }

  const overall = data.agents.every(
    (a) =>
      a.scope.status === "PASS" &&
      a.cost.status !== "FAIL" &&
      a.gates.status === "PASS" &&
      a.audit.status === "PASS"
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Governance Posture</h1>
          <p className="text-sm text-[var(--muted)]">
            {data.agent_count} agent{data.agent_count !== 1 ? "s" : ""} monitored
          </p>
        </div>
        <div className="flex items-center gap-3">
          <LiveBadge />
          <div className="flex items-center gap-2">
            <span className="text-sm">Overall:</span>
            <StatusBadge status={overall ? "PASS" : "WARN"} />
          </div>
        </div>
      </div>

      <StatsBar agents={data.agents} />

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" data-tour="posture">
        {data.agents.map((agent) => (
          <PostureCard key={agent.agent_id} agent={agent} />
        ))}
      </div>

      <p className="text-xs text-[var(--muted)] text-right">
        <TimeAgo iso={data.timestamp} />
      </p>
    </div>
  );
}
