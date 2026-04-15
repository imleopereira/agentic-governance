"use client";

/**
 * SessionsPanel — list of recent sessions for an agent.
 *
 * Pulls from `useAgentSessions(agentId)` which wraps
 * `/api/cost/sessions?agent_id=...`. No session data is synthesized.
 */

import { SectionLabel } from "../SectionLabel";
import { EmptyState } from "../EmptyState";
import { InfoBox } from "../InfoBox";
import { Pill } from "../Pill";
import { Skeleton } from "@/components/Skeleton";
import { useAgentSessions } from "@/hooks/useAgentQueries";
import { sanitizeErrorMessage } from "@/lib/connectionStore";
import { EMPTY_STATES, SESSIONS_PANEL_TITLE } from "@/lib/empty-states";

export interface SessionsPanelProps {
  agentId: string;
}

export function SessionsPanel({ agentId }: SessionsPanelProps) {
  const { data: sessions, isLoading, error } = useAgentSessions(agentId);

  if (isLoading) {
    // WCAG 4.1.3: use Skeleton, not EmptyState — a loading placeholder
    // is a different announcement category from "no data".
    return (
      <div className="space-y-3" aria-busy="true">
        <SectionLabel>{SESSIONS_PANEL_TITLE}</SectionLabel>
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-3">
        <SectionLabel>{SESSIONS_PANEL_TITLE}</SectionLabel>
        <InfoBox tone="danger" title="Failed to load sessions">
          {sanitizeErrorMessage((error as Error).message)}
        </InfoBox>
      </div>
    );
  }

  if (!sessions || sessions.length === 0) {
    return (
      <div className="space-y-3">
        <SectionLabel>{SESSIONS_PANEL_TITLE}</SectionLabel>
        <EmptyState
          title={EMPTY_STATES.sessionsPanel.title}
          description={EMPTY_STATES.sessionsPanel.description}
        />
      </div>
    );
  }

  return (
    <div>
      <SectionLabel>{SESSIONS_PANEL_TITLE} ({sessions.length})</SectionLabel>
      <ul>
        {sessions.map((s) => {
          const active = s.last_updated !== null;
          return (
            <li
              key={s.session_id}
              style={{
                background: "rgba(255,255,255,0.02)",
                borderRadius: 6,
                padding: "8px 10px",
                marginBottom: 5,
              }}
            >
              <div className="flex items-center justify-between">
                <div
                  className="font-mono truncate"
                  style={{ fontSize: 12, color: "var(--fg)" }}
                >
                  {s.session_id}
                </div>
                <Pill tone={active ? "success" : "neutral"}>
                  {active ? "active" : "closed"}
                </Pill>
              </div>
              <div
                className="font-mono flex gap-3"
                style={{
                  fontSize: 10.5,
                  color: "var(--text-tertiary)",
                  marginTop: 4,
                }}
              >
                <span>
                  {s.tokens_used.toLocaleString()} tok
                </span>
                <span>${s.usd_used.toFixed(2)}</span>
                <span>{s.last_updated ?? "\u2014"}</span>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
