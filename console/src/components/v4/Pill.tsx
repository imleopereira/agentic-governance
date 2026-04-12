"use client";

import type { ReactNode } from "react";

/** Semantic tone for a Pill. Maps to CSS custom properties only. */
export type PillTone =
  | "neutral"
  | "success"
  | "danger"
  | "warn"
  | "info"
  | "skip";

export interface PillProps {
  tone?: PillTone;
  children: ReactNode;
  title?: string;
}

const TONE_CLASS: Record<PillTone, string> = {
  neutral:
    "bg-[rgba(240,240,243,0.06)] text-[color:var(--fg)] border border-[rgba(240,240,243,0.10)]",
  success:
    "bg-[rgba(34,197,94,0.12)] text-[color:var(--success)] border border-[rgba(34,197,94,0.25)]",
  danger:
    "bg-[rgba(239,68,68,0.12)] text-[color:var(--danger)] border border-[rgba(239,68,68,0.25)]",
  warn:
    "bg-[rgba(234,179,8,0.12)] text-[color:var(--warn)] border border-[rgba(234,179,8,0.25)]",
  info:
    "bg-[rgba(96,165,250,0.12)] text-[color:var(--accent-light)] border border-[rgba(96,165,250,0.25)]",
  skip:
    "bg-transparent text-[color:var(--text-tertiary)] border border-dashed border-[rgba(240,240,243,0.10)]",
};

export function Pill({ tone = "neutral", children, title }: PillProps) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}
