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
import { useAgentSessions } from "@/hooks/useAgentQueries";
import { sanitizeErrorMessage } from "@/lib/connectionStore";

export interface SessionsPanelProps {
  agentId: string;
}

export function SessionsPanel({ agentId }: SessionsPanelProps) {
  const { data: sessions, isLoading, error } = useAgentSessions(agentId);

  if (isLoading) {
    return (
      <div className="space-y-3">
        <SectionLabel>Sessions</SectionLabel>
        <EmptyState
          title="Loading sessions..."
          description="Fetching session cost data."
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-3">
        <SectionLabel>Sessions</SectionLabel>
        <InfoBox tone="danger" title="Failed to load sessions">
          {sanitizeErrorMessage((error as Error).message)}
        </InfoBox>
      </div>
    );
  }

  if (!sessions || sessions.length === 0) {
    return (
      <div className="space-y-3">
        <SectionLabel>Sessions</SectionLabel>
        <EmptyState
          title="No sessions"
          description="No session-level cost data is available for this agent yet."
        />
      </div>
    );
  }

  return (
    <div>
      <SectionLabel>Sessions ({sessions.length})</SectionLabel>
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
