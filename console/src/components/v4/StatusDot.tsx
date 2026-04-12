"use client";

/**
 * StatusDot — M3-compliant status indicator.
 *
 * Never color-alone: every status renders as dot + glyph + optional label.
 * Shared primitive used by Agent R's list rows and drill sub-panels.
 *
 * v5 overlay: adds pulse/breathe animations keyed off status kind. Honors
 * prefers-reduced-motion via the global block in globals.css.
 */

import type { CSSProperties } from "react";

export type StatusKind =
  | "running"
  | "ready"
  | "degraded"
  | "blocked"
  | "failed"
  | "halted";

export interface StatusDotProps {
  status: StatusKind;
  size?: number;
  showLabel?: boolean;
}

interface StatusMeta {
  label: string;
  glyph: string;
  color: string; // CSS var reference
  glow: string; // rgba glow for pulse keyframe
  animation: "pulse" | "breathe" | "pulse-slow" | "none";
  dim?: boolean;
}

const STATUS_META: Record<StatusKind, StatusMeta> = {
  running: {
    label: "Running",
    glyph: "\u25B6",
    color: "var(--success)",
    glow: "rgba(34, 197, 94, 0.5)",
    animation: "pulse",
  },
  ready: {
    // Healthy but idle — not currently firing events. Rendered in the
    // dashboard-idle blue token so a glance at the list visually
    // separates "actively running right now" (pulsing green) from
    // "registered and healthy but quiet" (static blue). Prior version
    // used --success for both states, which made every healthy agent
    // look identical at the row-density we ship.
    label: "Idle",
    glyph: "\u25CB",
    color: "var(--status-idle)",
    glow: "rgba(59, 130, 246, 0.5)",
    animation: "none",
  },
  degraded: {
    label: "Degraded",
    glyph: "\u26A0",
    color: "var(--warn)",
    glow: "rgba(234, 179, 8, 0.5)",
    animation: "pulse-slow",
  },
  blocked: {
    label: "Blocked",
    glyph: "\u23F3",
    color: "var(--warn)",
    glow: "rgba(234, 179, 8, 0.5)",
    animation: "breathe",
  },
  failed: {
    label: "Failed",
    glyph: "\u2715",
    color: "var(--danger)",
    glow: "rgba(239, 68, 68, 0.5)",
    animation: "none",
    dim: true,
  },
  halted: {
    label: "Halted",
    glyph: "\u25A0",
    color: "var(--danger)",
    glow: "rgba(239, 68, 68, 0.5)",
    animation: "none",
    dim: true,
  },
};

function animationCss(kind: StatusMeta["animation"]): string | undefined {
  switch (kind) {
    case "pulse":
      return "v4-live-pulse 1.6s ease-in-out infinite";
    case "pulse-slow":
      return "v4-live-pulse 3s ease-in-out infinite";
    case "breathe":
      return "v4-breathe 2s ease-in-out infinite";
    case "none":
    default:
      return undefined;
  }
}

export function StatusDot({
  status,
  size = 10,
  showLabel = true,
}: StatusDotProps) {
  const meta = STATUS_META[status];
  const dotStyle: CSSProperties = {
    width: size,
    height: size,
    background: meta.color,
    // Custom prop drives the keyframe glow color.
    ["--v4-glow" as string]: meta.glow,
    animation: animationCss(meta.animation),
    opacity: meta.dim ? 0.4 : 1,
  };
  return (
    <span
      role="status"
      aria-label={`Status: ${meta.label}`}
      className="inline-flex items-center gap-1.5 text-xs"
      style={{ opacity: meta.dim ? 0.6 : 1 }}
    >
      <span
        aria-hidden="true"
        className="inline-block rounded-full"
        style={dotStyle}
      />
      <span aria-hidden="true" style={{ color: meta.color }}>
        {meta.glyph}
      </span>
      {showLabel && (
        <span style={{ color: "var(--fg)" }}>{meta.label}</span>
      )}
    </span>
  );
}
