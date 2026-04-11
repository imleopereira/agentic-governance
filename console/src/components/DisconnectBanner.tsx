"use client";
/**
 * SSE disconnect banner - shown at top of main content area when
 * the real-time connection is lost.
 */

import { useEventStreamStore } from "@/lib/store";

interface DisconnectBannerProps {
  onReconnect: () => void;
}

export function DisconnectBanner({ onReconnect }: DisconnectBannerProps) {
  const { connectionStatus, reconnectIn } = useEventStreamStore();

  if (connectionStatus === "connected" || connectionStatus === "connecting") {
    return null;
  }

  return (
    <div
      role="alert"
      aria-live="polite"
      style={{
        background: "rgba(239, 68, 68, 0.1)",
        borderBottom: "1px solid rgba(239, 68, 68, 0.25)",
        padding: "0.5rem 1.5rem",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "1rem",
        fontSize: "0.8125rem",
      }}
    >
      <span style={{ color: "var(--danger)", display: "flex", alignItems: "center", gap: "0.5rem" }}>
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
        Live connection lost.
        {reconnectIn > 0 ? (
          <span style={{ color: "rgba(239,68,68,0.7)" }}>
            Reconnecting in {reconnectIn}s&hellip;
          </span>
        ) : (
          <span style={{ color: "rgba(239,68,68,0.7)" }}>Reconnecting&hellip;</span>
        )}
      </span>
      <button
        onClick={onReconnect}
        style={{
          fontSize: "0.75rem",
          padding: "0.25rem 0.625rem",
          background: "rgba(239, 68, 68, 0.12)",
          border: "1px solid rgba(239, 68, 68, 0.35)",
          borderRadius: "var(--radius-sm)",
          color: "var(--danger)",
          cursor: "pointer",
          flexShrink: 0,
        }}
        aria-label="Reconnect to live stream now"
      >
        Reconnect now
      </button>
    </div>
  );
}
