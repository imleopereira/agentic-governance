"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function LiveBadge() {
  const { data } = useQuery({
    queryKey: ["posture"],
    queryFn: api.posture,
  });

  // Check if any agent has been active in the last 5 minutes
  const now = Date.now();
  const isLive = data?.agents?.some((agent) => {
    if (!agent.last_active) return false;
    const lastActive = new Date(agent.last_active).getTime();
    return now - lastActive < 5 * 60 * 1000; // 5 minutes
  });

  if (isLive) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-green-400">
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-green-500" />
        </span>
        Live
      </span>
    );
  }

  // No recent activity — show idle state
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: "var(--color-text-on-dark-tertiary)" }}>
      <span className="relative flex h-2 w-2">
        <span className="relative inline-flex rounded-full h-2 w-2 bg-gray-600" />
      </span>
      Idle
    </span>
  );
}
