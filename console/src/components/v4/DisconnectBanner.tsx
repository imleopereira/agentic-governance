"use client";
/**
 * v4 DisconnectBanner.
 *
 * Distinct from the v3 SSE-only DisconnectBanner at
 * `@/components/DisconnectBanner`: this one reflects *query* health
 * reported by the global QueryCache onError handler in
 * `@/lib/queryClient`, satisfying CTO invariant #1 (cached data stays
 * on screen while the backend is unreachable).
 *
 * Status-only — not dismissible.
 */

import { useConnectionStore } from "@/lib/connectionStore";

export function DisconnectBanner() {
  const status = useConnectionStore((s) => s.status);

  if (status === "ok") return null;

  const isOffline = status === "offline";
  const color = isOffline ? "var(--danger)" : "var(--warn)";
  const message = isOffline
    ? "Disconnected from governance backend. Showing last-known state."
    : "Partial data — some queries failing, showing cached results.";

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        background: isOffline
          ? "rgba(239, 68, 68, 0.10)"
          : "rgba(234, 179, 8, 0.10)",
        borderBottom: `1px solid ${color}`,
        padding: "0.5rem 1.5rem",
        display: "flex",
        alignItems: "center",
        gap: "0.625rem",
        fontSize: "0.8125rem",
        color,
      }}
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <circle cx="8" cy="8" r="7" />
        <line x1="8" y1="5" x2="8" y2="8" />
        <circle cx="8" cy="11" r="0.5" fill="currentColor" />
      </svg>
      <span>{message}</span>
    </div>
  );
}

export default DisconnectBanner;
