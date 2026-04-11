"use client";

import { useEffect, useState } from "react";

function formatRelative(iso: string): { text: string } {
  const d = new Date(iso);
  const now = Date.now();
  const diff = now - d.getTime();

  // Future timestamp (e.g. expires_at)
  if (diff < 0) {
    const absDiff = Math.abs(diff);
    const secs = Math.floor(absDiff / 1000);
    if (secs < 60) return { text: `in ${secs}s` };
    const mins = Math.floor(secs / 60);
    if (mins < 60) return { text: `in ${mins}m` };
    const hours = Math.floor(mins / 60);
    if (hours < 24) return { text: `in ${hours}h` };
    const days = Math.floor(hours / 24);
    return { text: `in ${days}d` };
  }

  // Past timestamp
  const secs = Math.floor(diff / 1000);
  if (secs < 5) return { text: "just now" };
  if (secs < 60) return { text: `${secs}s ago` };
  const mins = Math.floor(secs / 60);
  if (mins < 60) return { text: `${mins}m ago` };
  const hours = Math.floor(mins / 60);
  if (hours < 24) return { text: `${hours}h ago` };
  const days = Math.floor(hours / 24);
  return { text: `${days}d ago` };
}

export function TimeAgo({
  iso,
  label,
}: {
  iso: string | null;
  label?: string;
}) {
  const [display, setDisplay] = useState<{ text: string }>({ text: "" });

  useEffect(() => {
    if (!iso) return;
    setDisplay(formatRelative(iso));
    const interval = setInterval(() => setDisplay(formatRelative(iso)), 10_000);
    return () => clearInterval(interval);
  }, [iso]);

  if (!iso) return <span style={{ color: "var(--text-tertiary)" }}>-</span>;

  const fullIso = iso ? new Date(iso).toISOString() : "";

  return (
    <span className="relative group cursor-help">
      {label && (
        <span style={{ color: "var(--text-tertiary)" }}>{label} </span>
      )}
      {display.text}
      {fullIso && (
        <span
          className="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 whitespace-nowrap
                     rounded px-2 py-1 text-[10px] font-mono opacity-0 transition-opacity
                     group-hover:opacity-100 z-50"
          style={{
            background: "var(--elevated)",
            color: "var(--fg)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            boxShadow: "var(--shadow-card)",
          }}
        >
          {fullIso}
        </span>
      )}
    </span>
  );
}
