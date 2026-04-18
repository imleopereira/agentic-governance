/**
 * Demo usage numbers for the tier switcher. Hard-coded plausible values
 * so the prototype doesn't need real telemetry.
 */

export type TierName = "starter" | "team" | "enterprise";

export interface TierUsage {
  events_used: number;
  events_cap: number;
  agents_used: number;
  agents_cap: number | null;
  seats_used: number;
  seats_cap: number;
}

export const DEMO_USAGE: Record<TierName, TierUsage> = {
  starter: { events_used: 487_200, events_cap: 500_000, agents_used: 2, agents_cap: 2, seats_used: 1, seats_cap: 1 },
  team: { events_used: 2_140_800, events_cap: 5_000_000, agents_used: 4, agents_cap: 10, seats_used: 3, seats_cap: 5 },
  enterprise: { events_used: 12_800_000, events_cap: 50_000_000, agents_used: 23, agents_cap: null, seats_used: 9, seats_cap: 15 },
};

export const TIER_LABELS: Record<TierName, string> = {
  starter: "Starter", team: "Team", enterprise: "Enterprise",
};

export const TIER_PRICES: Record<TierName, string> = {
  starter: "$0", team: "$699/mo", enterprise: "$2,999/mo",
};
