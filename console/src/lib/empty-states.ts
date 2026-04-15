/**
 * Centralized empty-state copy for v4 drill panels.
 *
 * Single source of truth so the v0.6.1 i18n pass is a one-file change.
 * Every panel in `components/v4/drill/*` imports from here; do NOT
 * hardcode strings in panel components.
 */

export interface EmptyStateCopy {
  readonly title: string;
  readonly description: string;
}

export interface EmptyStates {
  readonly scopePanel: EmptyStateCopy;
  readonly sessionsPanel: EmptyStateCopy;
  readonly budgetPanel: EmptyStateCopy;
  readonly trailPanel: EmptyStateCopy;
}

export const EMPTY_STATES: EmptyStates = {
  scopePanel: {
    title: "Allowlist enforced by SDK",
    description:
      "This agent's tool allowlist is fail-closing on every call. Full policy read-back ships in v0.6.",
  },
  sessionsPanel: {
    title: "No sessions yet",
    description: "This agent hasn't logged any activity today.",
  },
  budgetPanel: {
    title: "No activity today",
    description: "Spend will appear once this agent makes its first call.",
  },
  trailPanel: {
    title: "No events yet",
    description: "This agent has not logged any events.",
  },
} as const;

export const SESSIONS_PANEL_TITLE = "Spend by session";
