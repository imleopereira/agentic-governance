"use client";

import type { ReactNode } from "react";

export type InfoBoxTone = "info" | "warn" | "danger";

export interface InfoBoxProps {
  tone?: InfoBoxTone;
  children: ReactNode;
  /** Optional title line rendered bolder above `children`. */
  title?: string;
}

const TONE: Record<
  InfoBoxTone,
  { bg: string; border: string; fg: string; glyph: string }
> = {
  info: {
    bg: "rgba(96, 165, 250, 0.08)",
    border: "rgba(96, 165, 250, 0.25)",
    fg: "var(--accent-light)",
    glyph: "\u2139",
  },
  warn: {
    bg: "rgba(234, 179, 8, 0.08)",
    border: "rgba(234, 179, 8, 0.25)",
    fg: "var(--warn)",
    glyph: "\u26A0",
  },
  danger: {
    bg: "rgba(239, 68, 68, 0.08)",
    border: "rgba(239, 68, 68, 0.25)",
    fg: "var(--danger)",
    glyph: "\u2715",
  },
};

export function InfoBox({ tone = "info", children, title }: InfoBoxProps) {
  const t = TONE[tone];
  // WCAG 4.1.3: danger-tone info boxes are alerts and must be
  // announced immediately; info/warn stay as passive `note` landmarks.
  const role = tone === "danger" ? "alert" : "note";
  return (
    <div
      role={role}
      className="flex gap-2 rounded-md border p-3 text-xs"
      style={{
        background: t.bg,
        borderColor: t.border,
        color: "var(--fg)",
      }}
    >
      <span aria-hidden="true" style={{ color: t.fg }}>
        {t.glyph}
      </span>
      <div className="flex-1">
        {title && (
          <div className="font-semibold mb-0.5" style={{ color: t.fg }}>
            {title}
          </div>
        )}
        <div>{children}</div>
      </div>
    </div>
  );
}
