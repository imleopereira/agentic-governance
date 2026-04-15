"use client";

/**
 * F4 Compliance Header Pill.
 *
 * A persistent top-right indicator that surfaces the current HMAC
 * chain-integrity status on every v4 console route. Four states:
 *
 *   - ``verified``    (green)
 *   - ``unverified``  (neutral)
 *   - ``degraded``    (yellow)
 *   - ``halted``      (red)
 *
 * DA blocker fix: the pill never shows a stale ``verified`` state. If
 * the last successful verify is older than ``STALE_THRESHOLD_MS`` the
 * pill neutralizes and announces "Verifying...".
 *
 * Cybersec MED: timestamps are rendered at MINUTE granularity only.
 * Millisecond precision leaks a scraping signal.
 *
 * Accessibility: ``aria-live="polite"`` announces status transitions;
 * the pulse animation is disabled when the user prefers reduced motion.
 *
 * Pure helpers live in ``ComplianceHeaderPill.utils.ts`` so vitest can
 * test them without a JSX transform.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Pill } from "./Pill";
import { api } from "@/lib/api";
import {
  type ComplianceChainStatus,
  formatVerifiedAt,
  isStale,
  prefersReducedMotion,
  statusLabel,
  statusToTone,
} from "./ComplianceHeaderPill.utils";

export {
  STALE_THRESHOLD_MS,
  formatVerifiedAt,
  isStale,
  statusLabel,
  statusToTone,
} from "./ComplianceHeaderPill.utils";
export type { ComplianceChainStatus } from "./ComplianceHeaderPill.utils";

export function ComplianceHeaderPill() {
  const [status, setStatus] = useState<ComplianceChainStatus>("unverified");
  const [verifiedAtMs, setVerifiedAtMs] = useState<number | null>(null);
  const [verifiedAtIso, setVerifiedAtIso] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [now, setNow] = useState<number>(() => Date.now());
  const inFlight = useRef(false);

  const reducedMotion = prefersReducedMotion();

  const reverify = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    try {
      const res = await api.verifyChain({});
      setStatus(res.chain_integrity_status);
      const ts = res.verified_at_utc ?? null;
      setVerifiedAtIso(ts);
      setVerifiedAtMs(ts ? new Date(ts).getTime() : Date.now());
    } catch {
      setStatus("unverified");
    } finally {
      setPending(false);
      inFlight.current = false;
    }
  }, []);

  // Initial load.
  useEffect(() => {
    reverify();
  }, [reverify]);

  // Refresh "now" every 30 seconds so the stale check re-evaluates
  // and the pill flips to "Verifying..." without a manual reload.
  useEffect(() => {
    const id = window.setInterval(() => {
      setNow(Date.now());
    }, 30_000);
    return () => window.clearInterval(id);
  }, []);

  // When the pill goes stale, kick off a fresh verify.
  useEffect(() => {
    if (isStale(verifiedAtMs, now) && !pending) {
      reverify();
    }
  }, [now, verifiedAtMs, pending, reverify]);

  const stale = pending || isStale(verifiedAtMs, now);
  const tone = statusToTone(status, stale);
  const label = statusLabel(status, stale);
  const ts = formatVerifiedAt(verifiedAtIso);
  const pulse = !reducedMotion && status === "halted" && !stale;

  return (
    <button
      type="button"
      onClick={reverify}
      aria-live="polite"
      aria-label={`Compliance ${label}, last verified ${ts}`}
      title={`Last verified ${ts} — click to re-verify`}
      className={`inline-flex items-center gap-2 border-0 bg-transparent p-0 ${
        pulse ? "animate-pulse" : ""
      }`}
      data-testid="compliance-header-pill"
      data-status={status}
      data-stale={stale ? "true" : "false"}
    >
      <Pill tone={tone} title={`Last verified ${ts}`}>
        {label}
        <span className="ml-2 opacity-70">{ts}</span>
      </Pill>
    </button>
  );
}
