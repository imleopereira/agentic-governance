"use client";

/**
 * ScopePanel — displays scope policy (allowed tools / apis / models).
 *
 * The `useAgentPolicy` hook is a stable shim until the `/api/policy/*`
 * endpoints ship (see comment in `@/hooks/useAgentQueries`). Until
 * then, this panel renders an empty state + info box rather than
 * guessing values.
 */

import { Pill } from "../Pill";
import { SectionLabel } from "../SectionLabel";
import { EmptyState } from "../EmptyState";
import { InfoBox } from "../InfoBox";
import { useAgentPolicy } from "@/hooks/useAgentQueries";
import { sanitizeErrorMessage } from "@/lib/connectionStore";

export interface ScopePolicyView {
  allowed_tools?: readonly string[];
  hidden_tools?: readonly string[];
  allowed_apis?: readonly string[];
  allowed_models?: readonly string[];
}

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

export function ScopePanel({ agentId }: ScopePanelProps) {
  const { data: policy, error } = useAgentPolicy(agentId) as {
    data: ScopePolicyView | undefined;
    error: Error | null;
  };

  if (error) {
    return (
      <div className="space-y-3">
        <SectionLabel>Scope Policy</SectionLabel>
        <InfoBox tone="danger" title="Failed to load policy">
          {sanitizeErrorMessage(error.message)}
        </InfoBox>
      </div>
    );
  }

  if (!policy) {
    return (
      <div className="space-y-4">
        <SectionLabel>Scope Policy</SectionLabel>
        <EmptyState
          title="Policy endpoint not available"
          description="Current scope is still enforced in-process by the SDK."
        />
        <InfoBox tone="info" title="Shipping in v0.6">
          The <code>/api/policy/*</code> endpoints will expose the live
          scope policy. Until then, the SDK&rsquo;s pre-call check
          fail-closes on anything not in the allowlist.
        </InfoBox>
      </div>
    );
  }

  const hasTools = (policy.allowed_tools?.length ?? 0) > 0;
  const hasHidden = (policy.hidden_tools?.length ?? 0) > 0;
  const hasApis = (policy.allowed_apis?.length ?? 0) > 0;
  const hasModels = (policy.allowed_models?.length ?? 0) > 0;

  return (
    <div className="space-y-5">
      <SectionLabel>Scope Policy</SectionLabel>

      {hasTools && (
        <div>
          <SectionLabel>
            Allowed Tools ({policy.allowed_tools!.length})
          </SectionLabel>
          <PillList items={policy.allowed_tools!} tone="success" />
        </div>
      )}

      {hasHidden && (
        <div>
          <SectionLabel>
            Hidden from Agent ({policy.hidden_tools!.length})
          </SectionLabel>
          <PillList items={policy.hidden_tools!} tone="danger" />
        </div>
      )}

      {hasApis && (
        <div>
          <SectionLabel>
            Allowed APIs ({policy.allowed_apis!.length})
          </SectionLabel>
          <PillList items={policy.allowed_apis!} tone="info" />
        </div>
      )}

      {hasModels && (
        <div>
          <SectionLabel>
            Allowed Models ({policy.allowed_models!.length})
          </SectionLabel>
          <PillList items={policy.allowed_models!} tone="info" />
        </div>
      )}

      <InfoBox tone="info">
        Enforcement: <strong>default-deny</strong> &middot; Violations logged to
        HMAC chain &middot; Hidden tools invisible to LLM
      </InfoBox>
    </div>
  );
}
