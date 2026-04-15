"use client";
/**
 * Unified disconnect banner (F2 P0).
 *
 * Combines two signals:
 *   1. `useConnectionStore.status` — REST/HTTP query health, set by the
 *      global QueryCache onError handler (see `@/lib/queryClient`).
 *      Values: `ok | degraded | offline`.
 *   2. `useEventStreamStore.connectionStatus` — SSE stream health from
 *      the useEventStream hook.
 *      Values: `connecting | connected | disconnected | polling`.
 *
 * Precedence (worst wins): `disconnected > reconnecting > connected`.
 *
 * Display strings:
 *   - "Live updates paused"          — SSE down, REST ok
 *   - "Console backend unreachable"  — REST down, SSE ok
 *   - "Console offline"              — both down
 *   - (no banner)                    — everything healthy
 *
 * The reconnect backoff timer is deliberately NOT displayed (Cybersec LOW
 * finding: leaks reconnect timing to attackers planning reconnect storms).
 */

import { useConnectionStore } from "@/lib/connectionStore";
import { useEventStreamStore } from "@/lib/store";
import { rankWorst, type Health } from "./DisconnectBanner.utils";

// Re-export so existing imports from "@/components/DisconnectBanner"
// keep working.
export { rankWorst };
export type { Health };

function restToHealth(status: "ok" | "degraded" | "offline"): Health {
  if (status === "ok") return "connected";
  if (status === "offline") return "disconnected";
  return "reconnecting";
}

function sseToHealth(
  status: "connecting" | "connected" | "disconnected" | "polling"
): Health {
  if (status === "connected" || status === "polling") return "connected";
  if (status === "disconnected") return "disconnected";
  return "reconnecting";
}

interface DisconnectBannerProps {
  /** Optional manual-reconnect handler. When omitted (e.g. v4 layout usage)
   *  the banner renders status-only with no action button. */
  onReconnect?: () => void;
}

export function DisconnectBanner({ onReconnect }: DisconnectBannerProps = {}) {
  const restStatus = useConnectionStore((s) => s.status);
  const sseStatus = useEventStreamStore((s) => s.connectionStatus);

  const rest = restToHealth(restStatus);
  const sse = sseToHealth(sseStatus);
  const worst = rankWorst(rest, sse);

  if (worst === "connected") return null;

  const restDown = rest !== "connected";
  const sseDown = sse !== "connected";

  let message: string;
  if (restDown && sseDown) {
    message = "Console offline";
  } else if (restDown) {
    message = "Console backend unreachable";
  } else {
    message = "Live updates paused";
  }

  const isCritical = worst === "disconnected";
  const color = isCritical ? "var(--danger)" : "var(--warn)";
  const bg = isCritical
    ? "rgba(239, 68, 68, 0.10)"
    : "rgba(234, 179, 8, 0.10)";

  return (
    <div
      role={isCritical ? "alert" : "status"}
      aria-live="polite"
      style={{
        background: bg,
        borderBottom: `1px solid ${color}`,
        padding: "0.5rem 1.5rem",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "1rem",
        fontSize: "0.8125rem",
        color,
      }}
    >
      <span style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
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
        {message}
      </span>
      {onReconnect ? (
        <button
          onClick={onReconnect}
          style={{
            fontSize: "0.75rem",
            padding: "0.25rem 0.625rem",
            background: isCritical
              ? "rgba(239, 68, 68, 0.12)"
              : "rgba(234, 179, 8, 0.12)",
            border: `1px solid ${color}`,
            borderRadius: "var(--radius-sm)",
            color,
            cursor: "pointer",
            flexShrink: 0,
          }}
          aria-label="Reconnect now"
        >
          Reconnect now
        </button>
      ) : null}
    </div>
  );
}

export default DisconnectBanner;
