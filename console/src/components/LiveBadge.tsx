"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function LiveBadge() {
  const { data } = useQuery({
    queryKey: ["posture"],
    queryFn: api.posture,
  });

  const now = Date.now();
  const isLive = data?.agents?.some((agent) => {
    if (!agent.last_active) return false;
    const lastActive = new Date(agent.last_active).getTime();
    return now - lastActive < 5 * 60 * 1000;
  });

  if (isLive) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: "var(--success)" }}>
        <span className="relative flex h-2 w-2">
          <span
            className="absolute inline-flex h-full w-full rounded-full opacity-75"
            style={{
              background: "var(--success)",
              animation: "pulse-ring 1.5s cubic-bezier(0, 0, 0.2, 1) infinite",
            }}
          />
          <span
            className="relative inline-flex rounded-full h-2 w-2"
            style={{ background: "var(--success)" }}
          />
        </span>
        Live
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: "var(--text-tertiary)" }}>
      <span className="relative flex h-2 w-2">
        <span className="relative inline-flex rounded-full h-2 w-2" style={{ background: "var(--text-tertiary)" }} />
      </span>
      Idle
    </span>
  );
}
