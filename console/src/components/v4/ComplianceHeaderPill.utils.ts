/**
 * F4 ComplianceHeaderPill — pure helper functions (non-JSX).
 *
 * Extracted from ``ComplianceHeaderPill.tsx`` so vitest can consume
 * them without pulling a React/JSX transform into the test runner.
 * The component module re-exports these identifiers for backward
 * compatibility with consumers that previously imported from the
 * ``.tsx`` surface.
 */
import type { PillTone } from "./Pill";

export type ComplianceChainStatus =
  | "verified"
  | "unverified"
  | "degraded"
  | "halted";

/**
 * Time after which a ``verified`` state is considered stale and the
 * pill forces a re-check. Five minutes balances "don't lie to the
 * user" against "don't thrash the backend every render".
 */
export const STALE_THRESHOLD_MS = 5 * 60 * 1000;

/** Map a chain status to the Pill tone the component should render. */
export function statusToTone(
  status: ComplianceChainStatus,
  isStale: boolean,
): PillTone {
  if (isStale) return "neutral";
  switch (status) {
    case "verified":
      return "success";
    case "degraded":
      return "warn";
    case "halted":
      return "danger";
    case "unverified":
    default:
      return "neutral";
  }
}

/**
 * Format a verification timestamp at MINUTE granularity.
 *
 * Cybersec MED fix: we deliberately strip seconds and milliseconds
 * because high-precision timestamps let a scraping adversary
 * correlate client polling patterns to backend verify latency.
 */
export function formatVerifiedAt(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "never";
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm} UTC`;
}

/**
 * True when the last verify is older than ``STALE_THRESHOLD_MS``
 * OR when ``verifiedAt`` is null (we have never verified).
 */
export function isStale(
  verifiedAtMs: number | null,
  nowMs: number,
  thresholdMs: number = STALE_THRESHOLD_MS,
): boolean {
  if (verifiedAtMs === null) return true;
  return nowMs - verifiedAtMs > thresholdMs;
}

/** Human-readable label for each status. */
export function statusLabel(
  status: ComplianceChainStatus,
  stale: boolean,
): string {
  if (stale) return "Verifying...";
  switch (status) {
    case "verified":
      return "Chain verified";
    case "degraded":
      return "Chain degraded";
    case "halted":
      return "Chain halted";
    case "unverified":
    default:
      return "Chain unverified";
  }
}

/** True when the user has ``prefers-reduced-motion: reduce`` set. */
export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}
