"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function LiveBadge() {
  const { data, dataUpdatedAt } = useQuery({
    queryKey: ["posture"],
    queryFn: api.posture,
  });
  const [showTooltip, setShowTooltip] = useState(false);

  const now = Date.now();
  const isLive = data?.agents?.some((agent) => {
    if (!agent.last_active) return false;
    const lastActive = new Date(agent.last_active).getTime();
    return now - lastActive < 5 * 60 * 1000;
  });

  const lastRefreshed = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString()
    : null;

  if (isLive) {
    return (
      <span
        className="relative inline-flex items-center gap-1.5 text-xs cursor-default"
        style={{ color: "var(--success)" }}
        onMouseEnter={() => setShowTooltip(true)}
        onMouseLeave={() => setShowTooltip(false)}
      >
        <span className="relative flex h-2 w-2">
          <span
            className="absolute inline-flex h-full w-full rounded-full"
            style={{
              background: "var(--success)",
              animation: "live-pulse 2s ease-in-out infinite",
            }}
          />
          <span
            className="relative inline-flex rounded-full h-2 w-2"
            style={{ background: "var(--success)" }}
          />
        </span>
        Live
        {showTooltip && lastRefreshed && (
          <span
            className="absolute top-full left-1/2 mt-1.5 -translate-x-1/2 whitespace-nowrap px-2 py-1 text-[10px] font-medium pointer-events-none"
            style={{
              background: "var(--elevated)",
              color: "var(--text-secondary)",
              borderRadius: "var(--radius-sm)",
              border: "1px solid var(--border)",
              boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
            }}
          >
            Last refreshed: {lastRefreshed}
          </span>
        )}
      </span>
    );
  }

  return (
    <span
      className="relative inline-flex items-center gap-1.5 text-xs cursor-default"
      style={{ color: "var(--text-tertiary)" }}
      onMouseEnter={() => setShowTooltip(true)}
      onMouseLeave={() => setShowTooltip(false)}
    >
      <span className="relative flex h-2 w-2">
        <span
          className="relative inline-flex rounded-full h-2 w-2"
          style={{ background: "var(--text-tertiary)" }}
        />
      </span>
      Idle
      {showTooltip && lastRefreshed && (
        <span
          className="absolute top-full left-1/2 mt-1.5 -translate-x-1/2 whitespace-nowrap px-2 py-1 text-[10px] font-medium pointer-events-none"
          style={{
            background: "var(--elevated)",
            color: "var(--text-secondary)",
            borderRadius: "var(--radius-sm)",
            border: "1px solid var(--border)",
            boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
          }}
        >
          Last refreshed: {lastRefreshed}
        </span>
      )}
    </span>
  );
}
