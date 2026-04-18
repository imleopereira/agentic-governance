/**
 * Centralized empty-state copy for v4 drill panels.
 *
 * Single source of truth so the v0.6.2 i18n pass is a one-file change.
 * Every panel in `components/v4/drill/*` imports from here; do NOT
 * hardcode strings in panel components.
 *
 * Voice: officer-voice. These strings are read by compliance officers
 * who know SOC 2 but do not know Python. No version callouts, no raw
 * code snippets, no dev-team in-jokes. One actionable sentence.
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
    title: "No scope policy set for this agent",
    description:
      "Until a policy is defined, every tool call is denied by default. A developer can set allowed tools via the governance SDK or from the Approvals page.",
  },
  sessionsPanel: {
    title: "No sessions yet",
    description: "This agent has not logged any activity today.",
  },
  budgetPanel: {
    title: "No spend recorded today",
    description:
      "Token and USD spend will appear here once this agent makes its first call.",
  },
  trailPanel: {
    title: "No audit events yet",
    description:
      "Events will appear here once this agent is wrapped with the governance SDK and begins making calls.",
  },
} as const;

export const SESSIONS_PANEL_TITLE = "Spend by session";
