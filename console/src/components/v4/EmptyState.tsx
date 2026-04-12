"use client";

import type { ReactNode } from "react";

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  /** Optional glyph — default is a minus sign. Icon-as-text for a11y. */
  glyph?: string;
  children?: ReactNode;
}

export function EmptyState({
  title,
  description,
  glyph = "\u2014",
  children,
}: EmptyStateProps) {
  return (
    <div
      className="flex flex-col items-center justify-center py-8 px-4 text-center rounded-md border border-dashed"
      style={{
        borderColor: "var(--border)",
        color: "var(--text-tertiary)",
      }}
    >
      <div
        aria-hidden="true"
        className="text-2xl mb-2"
        style={{ color: "var(--text-tertiary)" }}
      >
        {glyph}
      </div>
      <div
        className="text-sm font-medium"
        style={{ color: "var(--fg)" }}
      >
        {title}
      </div>
      {description && (
        <div className="text-xs mt-1 max-w-md">{description}</div>
      )}
      {children && <div className="mt-3">{children}</div>}
    </div>
  );
}
