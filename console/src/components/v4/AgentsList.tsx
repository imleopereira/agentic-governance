"use client";

/**
 * v4 Agents list — persistent across /agents ↔ /agents/[id] navigation.
 *
 * Lives in `(v4)/agents/layout.tsx`, not in `page.tsx`, so that when the
 * operator drills into an agent the list continues rendering on the left
 * while the `@drill` parallel route fills the right aside. See the
 * architectural note in layout.tsx for the parallel-routes pattern.
 */

import Link from "next/link";
import React from "react";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api, type PostureAgent } from "@/lib/api";
import { StatusDot } from "@/components/v4/StatusDot";
import { TimeAgo } from "@/components/TimeAgo";
import { Skeleton } from "@/components/Skeleton";
import { sanitizeErrorMessage } from "@/lib/connectionStore";
import { mapAgentStatus } from "@/lib/v4/statusMap";

const GRID_COLS_FULL = "1fr 180px 120px 90px 90px";
// F1 1280px fix: when the drill aside is open the list column is only
// ~840px wide. Collapse to a single "agent" column (no scope / spend /
// pending / events echoes) so agent_ids render without ellipsis. The
// compact row width targets 380px content. `GRID_COLS_FULL` is still
// used when no drill is open.
const GRID_COLS_COMPACT = "1fr";

interface AgentRowProps {
  agent: PostureAgent;
  compact: boolean;
}

const AgentRow = React.memo(function AgentRow({
  agent,
  compact,
}: AgentRowProps) {
  const status = mapAgentStatus(agent);
  const href = `/agents/${encodeURIComponent(agent.agent_id)}`;
  const pendingColor =
    agent.gates.pending > 0 ? "var(--warn)" : "var(--text-tertiary)";
  const violationsColor =
    agent.scope.violations_today > 0 ? "var(--danger)" : "var(--text-tertiary)";

  const ariaLabel = [
    `Agent ${agent.agent_id}`,
    `status ${status}`,
    `${agent.scope.violations_today} violations today`,
    `$${agent.cost.usd_today.toFixed(2)} spent today`,
    `${agent.gates.pending} pending approvals`,
    `${agent.audit.events_total} total events`,
  ].join(", ");

  // FIX 9: drop role="button" / tabIndex / onKeyDown — Link is already the
  // correct semantic element and adding role=button both confuses screen
  // readers and breaks Space activation.
  return (
    <Link
      href={href}
      aria-label={ariaLabel}
      className="agent-row grid items-center"
      style={{
        gridTemplateColumns: compact ? GRID_COLS_COMPACT : GRID_COLS_FULL,
        padding: "12px 28px",
        borderBottom: "1px solid var(--border)",
        borderLeft: "3px solid transparent",
        color: "var(--fg)",
        textDecoration: "none",
        transition:
          "background-color var(--transition-fast), border-left-color var(--transition-fast)",
      }}
    >
      <div className="min-w-0">
        <StatusDot status={status} />
        <div
          className="font-mono truncate"
          style={{
            fontSize: 14,
            fontWeight: 600,
            marginTop: 4,
            color: "var(--fg)",
          }}
        >
          {agent.agent_id}
        </div>
        <div
          className="font-mono"
          style={{
            fontSize: 11,
            color: "var(--text-tertiary)",
            marginTop: 2,
          }}
        >
          {agent.last_active ? (
            <>
              last active <TimeAgo iso={agent.last_active} />
            </>
          ) : (
            "no activity yet"
          )}
        </div>
      </div>

      {compact ? null : (
      <>
      <div className="font-mono" style={{ fontSize: 12 }}>
        <span style={{ color: violationsColor }}>
          {agent.scope.violations_today}
        </span>{" "}
        <span style={{ color: "var(--text-tertiary)" }}>violations today</span>
      </div>

      <div>
        <div
          className="font-mono"
          style={{ fontSize: 13, fontWeight: 600, color: "var(--fg)" }}
        >
          ${agent.cost.usd_today.toFixed(2)}
        </div>
        <div
          className="font-mono"
          style={{ fontSize: 10, color: "var(--text-tertiary)" }}
        >
          today
        </div>
      </div>

      <div
        className="font-mono"
        style={{ fontSize: 12, color: pendingColor, textAlign: "right" }}
      >
        {agent.gates.pending}
      </div>

      <div
        className="font-mono"
        style={{ fontSize: 12, color: "var(--fg)", textAlign: "right" }}
      >
        {agent.audit.events_total.toLocaleString()}
      </div>
      </>
      )}
    </Link>
  );
});

// TODO(F1): once Playwright is in devDependencies, add a 1280x800
// snapshot test at `console/tests/playwright/agents-list-1280.spec.ts`
// asserting compact mode renders the first 5 agent_ids without ellipsis
// when the drill drawer is open. Playwright is NOT yet in
// devDependencies so the snapshot is deferred.
export default function AgentsList() {
  const pathname = usePathname();
  // Drawer-open signal is the URL (see `(v4)/agents/layout.tsx`).
  const compact = /^\/agents\/[^/]+/.test(pathname ?? "");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["posture"],
    queryFn: () => api.posture(),
    refetchInterval: 5000,
    // FIX 12: hold previous page's data during refetch so the list does
    // not flash empty/loading every 5s.
    placeholderData: (previousData) => previousData,
  });

  const agents: PostureAgent[] = data?.agents ?? [];

  return (
    <section className="py-6">
      <style>{`
        .agent-row:hover {
          background-color: rgba(255,255,255,0.03);
          border-left-color: var(--success);
        }
        .agent-row:focus-visible {
          background-color: rgba(255,255,255,0.04);
        }
      `}</style>
      <header className="mb-6 flex items-baseline justify-between px-7">
        <div>
          <h1 className="text-xl font-semibold">Agents</h1>
          <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
            {data
              ? `${data.agent_count} agent${data.agent_count === 1 ? "" : "s"}`
              : "Loading posture..."}
          </p>
        </div>
      </header>

      {isLoading && (
        // FIX 10: skeleton rows matching AgentRow grid.
        <div>
          {Array.from({ length: 5 }).map((_, i) => (
            <div
              key={i}
              className="grid items-center"
              style={{
                gridTemplateColumns: compact ? GRID_COLS_COMPACT : GRID_COLS_FULL,
                padding: "12px 28px",
                borderBottom: "1px solid var(--border)",
                gap: 8,
              }}
            >
              <Skeleton className="h-4 w-48" />
              {!compact && <Skeleton className="h-3 w-28" />}
              {!compact && <Skeleton className="h-4 w-16" />}
              {!compact && <Skeleton className="h-3 w-10" />}
              {!compact && <Skeleton className="h-3 w-12" />}
            </div>
          ))}
        </div>
      )}

      {isError && (
        <div
          className="card mx-7 p-4 text-sm"
          style={{ color: "var(--danger)" }}
        >
          {/* FIX 4: sanitize raw error text before rendering. */}
          Failed to load agents:{" "}
          {sanitizeErrorMessage((error as Error)?.message ?? "unknown error")}
        </div>
      )}

      {!isLoading && !isError && agents.length === 0 && (
        <div
          className="card mx-7 p-4 text-sm"
          style={{ color: "var(--text-secondary)" }}
        >
          No agents recorded yet.
        </div>
      )}

      {agents.length > 0 && (
        <div>
          <div
            role="row"
            className="grid items-center font-mono uppercase"
            style={{
              gridTemplateColumns: compact ? GRID_COLS_COMPACT : GRID_COLS_FULL,
              padding: "8px 28px",
              borderBottom: "1px solid var(--border)",
              fontSize: 10.5,
              letterSpacing: "0.08em",
              color: "var(--text-tertiary)",
            }}
          >
            <div>Agent</div>
            {!compact && <div>Scope</div>}
            {!compact && <div>Spend</div>}
            {!compact && <div style={{ textAlign: "right" }}>Pending</div>}
            {!compact && <div style={{ textAlign: "right" }}>Events</div>}
          </div>

          {agents.map((a) => (
            <AgentRow key={a.agent_id} agent={a} compact={compact} />
          ))}
        </div>
      )}
    </section>
  );
}
