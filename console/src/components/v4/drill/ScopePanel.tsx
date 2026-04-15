"use client";

/**
 * ScopePanel — renders the live scope policy for an agent.
 *
 * Wired to F3's `GET /api/policies/{agent_id}` via `useAgentPolicy`.
 * The hook returns a `ScopePolicyView` that has already been narrowed
 * (every string in the list is a `typeof "string"` at runtime). No
 * `as` cast, no dict passthrough — anything the backend leaks that
 * isn't a string tool name is dropped before it can reach the DOM.
 */

import { Pill } from "../Pill";
import { SectionLabel } from "../SectionLabel";
import { EmptyState } from "../EmptyState";
import { InfoBox } from "../InfoBox";
import {
  useAgentPolicy,
  type ScopePolicyView,
} from "@/hooks/useAgentQueries";
import { sanitizeErrorMessage } from "@/lib/connectionStore";
import { EMPTY_STATES } from "@/lib/empty-states";

export type { ScopePolicyView };

export interface ScopePanelProps {
  agentId: string;
}

function PillList({
  items,
  tone = "success",
}: {
  items: readonly string[];
  tone?: "success" | "danger" | "info";
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {items.map((item) => (
        <Pill key={item} tone={tone}>
          {item}
        </Pill>
      ))}
    </div>
  );
}

function isEmptyPolicy(p: ScopePolicyView | undefined): boolean {
  if (!p) return true;
  return (
    p.allowed_tools.length === 0 &&
    p.hidden_tools.length === 0 &&
    p.allowed_apis.length === 0 &&
    p.allowed_models.length === 0
  );
}

export function ScopePanel({ agentId }: ScopePanelProps) {
  const { data: policy, error, isLoading } = useAgentPolicy(agentId);

  if (isLoading) {
    return (
      <div className="space-y-3">
        <h3 className="text-xs font-mono uppercase tracking-wide">
          Scope Policy
        </h3>
        <div className="text-xs" style={{ color: "var(--text-tertiary)" }}>
          Loading policy...
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-3">
        <h3 className="text-xs font-mono uppercase tracking-wide">
          Scope Policy
        </h3>
        <InfoBox tone="danger" title="Failed to load policy">
          {sanitizeErrorMessage(error.message)}
        </InfoBox>
      </div>
    );
  }

  if (isEmptyPolicy(policy)) {
    return (
      <div className="space-y-4">
        <h3 className="text-xs font-mono uppercase tracking-wide">
          Scope Policy
        </h3>
        <EmptyState
          title={EMPTY_STATES.scopePanel.title}
          description={EMPTY_STATES.scopePanel.description}
        />
      </div>
    );
  }

  const p = policy as ScopePolicyView;
  const hasTools = p.allowed_tools.length > 0;
  const hasHidden = p.hidden_tools.length > 0;
  const hasApis = p.allowed_apis.length > 0;
  const hasModels = p.allowed_models.length > 0;

  return (
    <div className="space-y-5">
      <h3 className="text-xs font-mono uppercase tracking-wide">
        Scope Policy
      </h3>

      {hasTools && (
        <div>
          <SectionLabel>
            Allowed Tools ({p.allowed_tools.length})
          </SectionLabel>
          <PillList items={p.allowed_tools} tone="success" />
        </div>
      )}

      {hasHidden && (
        <div>
          <SectionLabel>
            Hidden from Agent ({p.hidden_tools.length})
          </SectionLabel>
          <PillList items={p.hidden_tools} tone="danger" />
        </div>
      )}

      {hasApis && (
        <div>
          <SectionLabel>
            Allowed APIs ({p.allowed_apis.length})
          </SectionLabel>
          <PillList items={p.allowed_apis} tone="info" />
        </div>
      )}

      {hasModels && (
        <div>
          <SectionLabel>
            Allowed Models ({p.allowed_models.length})
          </SectionLabel>
          <PillList items={p.allowed_models} tone="info" />
        </div>
      )}

      <InfoBox tone="info">
        Enforcement: <strong>default-deny</strong> &middot; Violations logged to
        HMAC chain &middot; Hidden tools invisible to LLM
      </InfoBox>
    </div>
  );
}
