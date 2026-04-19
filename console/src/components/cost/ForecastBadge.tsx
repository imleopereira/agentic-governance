"use client";

/**
 * ForecastBadge — "cap in 3.2 days" projection chip.
 *
 * PRD §F2 requirement: hidden when <30 days of data are available. That
 * constraint is explicit because a naive 3-day linear projection on a
 * brand-new tenant would read "cap in 0.2 days" the moment the first call
 * lands and spam the compliance officer with ghost alerts.
 *
 * Inputs:
 *   * currentUsd          — cumulative USD spent toward the cap window
 *   * capUsd              — the USD ceiling for that window
 *   * dailyBurnUsd        — average USD/day across the observed history
 *   * daysOfHistory       — how many days of observations we have
 *
 * We render nothing if:
 *   * daysOfHistory < 30 (insufficient signal)
 *   * dailyBurnUsd <= 0 (no projection possible)
 *   * currentUsd >= capUsd (already over — a different badge's job)
 *
 * Colorway:
 *   * ≤ 7 days to cap   → danger red
 *   * ≤ 30 days to cap  → warn amber
 *   * otherwise         → accent neutral (still informative)
 */

import { TrendingUp, AlertCircle } from "lucide-react";

export interface ForecastBadgeProps {
  currentUsd: number;
  capUsd: number;
  dailyBurnUsd: number;
  daysOfHistory: number;
  /** Minimum days of history required to show. Default 30. */
  minDays?: number;
}

export function ForecastBadge({
  currentUsd,
  capUsd,
  dailyBurnUsd,
  daysOfHistory,
  minDays = 30,
}: ForecastBadgeProps) {
  if (daysOfHistory < minDays) return null;
  if (dailyBurnUsd <= 0) return null;
  if (currentUsd >= capUsd) return null;

  const remainingUsd = capUsd - currentUsd;
  const daysToCap = remainingUsd / dailyBurnUsd;
  if (!isFinite(daysToCap) || daysToCap <= 0) return null;

  const bucket = daysToCap <= 7 ? "danger" : daysToCap <= 30 ? "warn" : "ok";
  const color =
    bucket === "danger"
      ? "var(--danger)"
      : bucket === "warn"
        ? "var(--warn)"
        : "var(--accent)";
  const bg =
    bucket === "danger"
      ? "rgba(239, 68, 68, 0.10)"
      : bucket === "warn"
        ? "rgba(234, 179, 8, 0.10)"
        : "rgba(130, 40, 245, 0.10)";
  const border =
    bucket === "danger"
      ? "rgba(239, 68, 68, 0.35)"
      : bucket === "warn"
        ? "rgba(234, 179, 8, 0.35)"
        : "rgba(130, 40, 245, 0.35)";

  const Icon = bucket === "danger" ? AlertCircle : TrendingUp;
  const formatted =
    daysToCap >= 10 ? daysToCap.toFixed(0) : daysToCap.toFixed(1);

  return (
    <span
      role="status"
      aria-label={`Projected to hit cap in ${formatted} days`}
      title={`Based on ${daysOfHistory}-day rolling average burn of $${dailyBurnUsd.toFixed(2)}/day`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 10px",
        fontSize: "0.75rem",
        fontWeight: 500,
        background: bg,
        color: color,
        border: `1px solid ${border}`,
        borderRadius: 999,
      }}
    >
      <Icon size={12} aria-hidden="true" />
      Cap in {formatted} {daysToCap === 1 ? "day" : "days"}
    </span>
  );
}

export default ForecastBadge;
