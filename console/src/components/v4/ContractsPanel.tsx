"use client";

/**
 * ContractsPanel — PRD B2: empty state ALWAYS.
 *
 * Do NOT render simulated contract data. The contracts API endpoint ships in
 * v0.6; until then we show a friction-free empty state pointing at the SDK.
 */

import { EmptyState } from "./EmptyState";
import { InfoBox } from "./InfoBox";
import { SectionLabel } from "./SectionLabel";

export interface ContractsPanelProps {
  agentId: string;
}

// FIX 13: the panel is per-agent even though we don't use the id until
// the contracts API ships in v0.6. Take the prop under its real name so
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
        The contracts API endpoint ships in v0.6.
      </InfoBox>
    </div>
  );
}
