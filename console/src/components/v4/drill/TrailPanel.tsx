"use client";

/**
 * TrailPanel — recent audit events for an agent (v5 visual overlay).
 *
 * Fetches its own data via `useAgentTrail`. Loading / error / empty /
 * success branches unchanged; only the success render is restyled.
 */

import { SectionLabel } from "../SectionLabel";
import { EmptyState } from "../EmptyState";
import { InfoBox } from "../InfoBox";
import { Skeleton } from "@/components/Skeleton";
import { useAgentTrail } from "@/hooks/useAgentQueries";
import { sanitizeErrorMessage } from "@/lib/connectionStore";
import { EMPTY_STATES } from "@/lib/empty-states";
import type { AuditEvent } from "@/lib/api";

export interface TrailPanelProps {
  agentId: string;
}

const SEVERITY_COLOR: Record<string, string> = {
  normal: "var(--text-secondary)",
  danger: "var(--danger)",
  warn: "var(--warn)",
  hitl: "var(--status-hitl)",
};

function severityFor(kind: string): keyof typeof SEVERITY_COLOR {
  if (kind.startsWith("scope")) return "danger";
  if (kind.startsWith("budget")) return "warn";
  if (kind.startsWith("hitl") || kind.startsWith("gate")) return "hitl";
  return "normal";
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(11, 19);
  return d.toISOString().slice(11, 19);
}

function detailFor(e: AuditEvent): string {
  if (e.model) return e.model;
  if (e.session_id) return `session ${e.session_id.slice(0, 8)}`;
  return `seq ${e.chain_seq}`;
}

export function TrailPanel({ agentId }: TrailPanelProps) {
  const { data: events, isLoading, error } = useAgentTrail(agentId);

  if (isLoading) {
    // WCAG 4.1.3: loading is a Skeleton, not an EmptyState.
    return (
      <div className="space-y-3" aria-busy="true">
        <SectionLabel>Recent Events</SectionLabel>
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-4 w-4/6" />
        <Skeleton className="h-4 w-3/4" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-3">
        <SectionLabel>Recent Events</SectionLabel>
        <InfoBox tone="danger" title="Failed to load trail">
          {sanitizeErrorMessage((error as Error).message)}
        </InfoBox>
      </div>
    );
  }

  if (!events || events.length === 0) {
    return (
      <div className="space-y-3">
        <SectionLabel>Recent Events</SectionLabel>
        <EmptyState
          title={EMPTY_STATES.trailPanel.title}
          description={EMPTY_STATES.trailPanel.description}
        />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <SectionLabel>Recent Events ({events.length})</SectionLabel>
      <ol>
        {events.map((e) => {
          const sev = severityFor(e.kind);
          const color = SEVERITY_COLOR[sev];
          return (
            <li
              key={e.event_id}
              className="flex"
              style={{
                padding: "6px 0",
                borderBottom: "1px solid var(--border)",
                gap: 10,
              }}
            >
              <div
                className="font-mono shrink-0"
                style={{
                  width: 62,
                  fontSize: 11,
                  color: "var(--text-tertiary)",
                }}
              >
                {formatTimestamp(e.created_at)}
              </div>
              <div className="min-w-0 flex-1">
                <div
                  className="font-mono uppercase"
                  style={{
                    fontSize: 9.5,
                    letterSpacing: "0.04em",
                    color,
                  }}
                >
                  {e.kind}
                </div>
                <div
                  className="font-mono truncate"
                  style={{
                    fontSize: 12,
                    color: "var(--fg)",
                    marginTop: 1,
                  }}
                >
                  {detailFor(e)}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
