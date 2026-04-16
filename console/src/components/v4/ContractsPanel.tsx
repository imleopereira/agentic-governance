"use client";

/**
 * ContractsPanel — PRD B2: empty state ALWAYS.
 *
 * Do NOT render simulated contract data. Contracts are registered via
 * the SDK in-process; this panel shows the pointer, not a synthetic
 * list. A per-agent read endpoint that would let the console enumerate
 * registered contracts is on the v0.7 roadmap.
 */

import { EmptyState } from "./EmptyState";
import { InfoBox } from "./InfoBox";
import { SectionLabel } from "./SectionLabel";

export interface ContractsPanelProps {
  agentId: string;
}

// FIX 13: the panel is per-agent even though we don't use the id until
// the contracts read endpoint lands. Take the prop under its real name so
// callers (and future maintainers) aren't misled by a `_agentId`
// convention that suggests it is genuinely unused at the design level.
// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function ContractsPanel({ agentId }: ContractsPanelProps) {
  return (
    <div className="space-y-4">
      <SectionLabel>Contracts</SectionLabel>
      <EmptyState
        title="No contracts visible."
        description={
          <span className="font-mono">
            Register via SDK: sdk.contracts.register(Contract(...))
          </span>
        }
      />
      <InfoBox tone="info">
        Contracts are registered in-process via the SDK. A per-agent
        read endpoint is on the v0.7 roadmap.
      </InfoBox>
    </div>
  );
}
