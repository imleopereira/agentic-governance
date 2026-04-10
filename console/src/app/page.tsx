"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type PostureAgent } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { CardSkeleton } from "@/components/Skeleton";
import { LiveBadge } from "@/components/LiveBadge";
import { TimeAgo } from "@/components/TimeAgo";
import { GettingStarted } from "@/components/GettingStarted";

function ViolationDetails({ agent }: { agent: PostureAgent }) {
  const [expanded, setExpanded] = useState(false);
  const violation = agent.scope.latest_violation;

  if (agent.scope.violations_today <= 0 || !violation) {
    return null;
  }

  return (
    <div className="text-xs">
      <button
        onClick={(e) => {
          e.preventDefault();
          setExpanded((v) => !v);
        }}
        className="flex items-center gap-1 transition-colors"
        style={{ color: "var(--danger)" }}
      >
        <span>{agent.scope.violations_today} scope violation(s) today</span>
        <span style={{ color: "var(--text-tertiary)" }}>{expanded ? "\u25B2" : "\u25BC"}</span>
      </button>
      {expanded && (
        <div
          className="mt-1.5 p-2 border"
          style={{
            background: "rgba(239, 68, 68, 0.06)",
            borderColor: "rgba(239, 68, 68, 0.15)",
            borderRadius: "var(--radius-sm)",
            color: "var(--text-tertiary)",
          }}
        >
          <p>
            Latest: attempted{" "}
            <span className="font-mono" style={{ color: "var(--fg)" }}>
              &apos;{violation.tool}&apos;
            </span>
            {violation.created_at && (
              <>
                {" "}&mdash;{" "}
                <TimeAgo iso={violation.created_at} />
              </>
            )}
          </p>
        </div>
      )}
    </div>
  );
}

function PostureCard({ agent }: { agent: PostureAgent }) {
  return (
    <a
      href={`/events?agent_id=${encodeURIComponent(agent.agent_id)}`}
      className="block border p-4 space-y-3 transition-all hover:shadow-lg"
      style={{
        borderColor: "var(--border)",
        background: "var(--card)",
        borderRadius: "var(--radius-md)",
      }}
    >
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-sm truncate">{agent.agent_id}</h3>
        <span className="text-xs" style={{ color: "var(--text-tertiary)" }}>
          {agent.event_count.toLocaleString()} events
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs" data-tour="status-badge">
        {(["Scope", "Cost", "Gates", "Audit"] as const).map((label) => {
          const key = label.toLowerCase() as "scope" | "cost" | "gates" | "audit";
          const status = key === "audit" ? agent.audit.status : agent[key].status;
          return (
            <div
              key={label}
              className="flex items-center justify-between p-2"
              style={{
                background: "rgba(0, 0, 0, 0.2)",
                borderRadius: "var(--radius-sm)",
              }}
            >
              <span>{label}</span>
              <StatusBadge status={status} />
            </div>
          );
        })}
      </div>

      {agent.cost.usd_today > 0 && (
        <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>
          Today: ${agent.cost.usd_today.toFixed(4)} /{" "}
          {agent.cost.tokens_today.toLocaleString()} tokens
        </p>
      )}
      <ViolationDetails agent={agent} />
      {agent.gates.pending > 0 && (
        <p className="text-xs" style={{ color: "var(--warn)" }}>
          {agent.gates.pending} approval(s) pending
        </p>
      )}
      {agent.last_active && (
        <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>
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

  const stats = [
    { label: "Total events", value: totalEvents.toLocaleString() },
    { label: "Spend today", value: `$${totalSpend.toFixed(4)}` },
    {
      label: "Pending approvals",
      value: String(pendingGates),
      highlight: pendingGates > 0,
    },
  ];

  return (
    <div className="grid grid-cols-3 gap-4">
      {stats.map((s) => (
        <div
          key={s.label}
          className="border p-3 text-center"
          style={{
            borderColor: "var(--border)",
            background: "var(--card)",
            borderRadius: "var(--radius-md)",
          }}
        >
          <p
            className="text-2xl font-bold font-mono"
            style={{ color: s.highlight ? "var(--warn)" : "var(--fg)" }}
          >
            {s.value}
          </p>
          <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>
            {s.label}
          </p>
        </div>
      ))}
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
            <h1 className="text-2xl font-bold tracking-tight">Governance Posture</h1>
            <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
              Loading...
            </p>
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
        <div className="inline-flex items-center justify-center w-12 h-12 mb-4 border" style={{ borderColor: "rgba(239, 68, 68, 0.3)", borderRadius: "var(--radius-md)", background: "rgba(239, 68, 68, 0.08)" }}>
          <span className="text-xl" style={{ color: "var(--danger)" }}>!</span>
        </div>
        <h2 className="text-xl mb-2" style={{ color: "var(--danger)" }}>
          Connection error
        </h2>
        <p className="text-sm mb-4" style={{ color: "var(--text-tertiary)" }}>
          Could not reach the governance API at /api/posture.
        </p>
        <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>
          Make sure the backend is running:{" "}
          <code
            className="px-1.5 py-0.5 font-mono text-xs"
            style={{
              background: "rgba(130, 40, 245, 0.1)",
              border: "1px solid rgba(130, 40, 245, 0.2)",
              borderRadius: "3px",
            }}
          >
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
          <h1 className="text-2xl font-bold tracking-tight">Governance Posture</h1>
          <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            {data.agent_count} agent{data.agent_count !== 1 ? "s" : ""} monitored
          </p>
        </div>
        <div className="flex items-center gap-3">
          <LiveBadge />
          <div className="flex items-center gap-2">
            <span className="text-sm" style={{ color: "var(--text-secondary)" }}>
              Overall:
            </span>
            <StatusBadge status={overall ? "PASS" : "WARN"} />
          </div>
        </div>
      </div>

      <StatsBar agents={data.agents} />

      <div
        className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"
        data-tour="posture"
      >
        {data.agents.map((agent) => (
          <PostureCard key={agent.agent_id} agent={agent} />
        ))}
      </div>

      <p className="text-xs text-right" style={{ color: "var(--text-tertiary)" }}>
        <TimeAgo iso={data.timestamp} />
      </p>
    </div>
  );
}
