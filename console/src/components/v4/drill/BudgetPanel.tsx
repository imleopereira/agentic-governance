"use client";

/**
 * BudgetPanel — current daily spend + policy grid. NO sparkline (CTO B1).
 *
 * Pulls `PostureAgent.cost` via `useAgent(agentId)`; no session-level
 * cap bar yet (shipping with policy endpoints in v0.6).
 */

import { SectionLabel } from "../SectionLabel";
import { InfoBox } from "../InfoBox";
import { EmptyState } from "../EmptyState";
import { useAgent } from "@/hooks/useAgentQueries";
import { sanitizeErrorMessage } from "@/lib/connectionStore";

export interface BudgetPanelProps {
  agentId: string;
}

export function BudgetPanel({ agentId }: BudgetPanelProps) {
  const { data: agent, isLoading, error } = useAgent(agentId);

  if (isLoading) {
    return (
      <div className="space-y-3">
        <SectionLabel>Budget</SectionLabel>
        <EmptyState
          title="Loading budget..."
          description="Fetching posture from governance backend."
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-3">
        <SectionLabel>Budget</SectionLabel>
        <InfoBox tone="danger" title="Failed to load budget">
          {sanitizeErrorMessage((error as Error).message)}
        </InfoBox>
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="space-y-3">
        <SectionLabel>Budget</SectionLabel>
        <EmptyState
          title="Agent not found in posture"
          description="No cost data available for this agent."
        />
      </div>
    );
  }

  const { cost } = agent;
  const exceeded = cost.exceeded_today > 0;

  // FIX 8: the previous implementation rendered a magic `35%` progress
  // bar when there was ANY spend — pure fabrication, no cap field
  // exists on `PostureAgent`. The bar is removed; an InfoBox below
  // calls out that session caps ship in v0.6.

  return (
    <div className="space-y-5">
      <SectionLabel>Budget</SectionLabel>

      {/* Large-number display */}
      <div>
        <div
          className="font-mono"
          style={{
            fontSize: 22,
            fontWeight: 700,
            color: exceeded ? "var(--danger)" : "var(--fg)",
            lineHeight: 1.1,
          }}
        >
          ${cost.usd_today.toFixed(2)}
        </div>
        <div
          className="font-mono"
          style={{
            fontSize: 10,
            color: "var(--text-tertiary)",
            marginTop: 2,
          }}
        >
          today
        </div>
      </div>

      <InfoBox tone="info">
        Session cap not available — shipping in v0.6.
      </InfoBox>

      {/* Policy grid */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <div
            className="font-mono uppercase"
            style={{
              fontSize: 9,
              letterSpacing: "0.1em",
              color: "var(--text-tertiary)",
            }}
          >
            Tokens today
          </div>
          <div
            className="font-mono"
            style={{ fontSize: 12, color: "var(--fg)", marginTop: 2 }}
          >
            {cost.tokens_today.toLocaleString()}
          </div>
        </div>
        <div>
          <div
            className="font-mono uppercase"
            style={{
              fontSize: 9,
              letterSpacing: "0.1em",
              color: "var(--text-tertiary)",
            }}
          >
            Status
          </div>
          <div
            className="font-mono"
            style={{
              fontSize: 12,
              marginTop: 2,
              color: exceeded ? "var(--danger)" : "var(--success)",
            }}
          >
            {cost.status}
          </div>
        </div>
        <div>
          <div
            className="font-mono uppercase"
            style={{
              fontSize: 9,
              letterSpacing: "0.1em",
              color: "var(--text-tertiary)",
            }}
          >
            Exceeded today
          </div>
          <div
            className="font-mono"
            style={{
              fontSize: 12,
              marginTop: 2,
              color: exceeded ? "var(--danger)" : "var(--fg)",
            }}
          >
            {cost.exceeded_today}
          </div>
        </div>
        <div>
          <div
            className="font-mono uppercase"
            style={{
              fontSize: 9,
              letterSpacing: "0.1em",
              color: "var(--text-tertiary)",
            }}
          >
            Session cap
          </div>
          <div
            className="font-mono"
            style={{
              fontSize: 12,
              marginTop: 2,
              color: "var(--text-tertiary)",
            }}
          >
            v0.6
          </div>
        </div>
      </div>

      <InfoBox tone="info">
        Enforcement: <strong>fail-closed</strong> &middot; Pre-call check blocks
        if any cap exceeded &middot; 25 models auto-priced
      </InfoBox>
    </div>
  );
}
