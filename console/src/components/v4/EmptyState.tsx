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
  // WCAG 4.1.3 Status Messages: announce the empty state to AT without
  // stealing focus. WCAG 1.3.1 Info and Relationships: the title is a
  // heading so the page outline actually reflects the semantic section.
  return (
    <div
      role="status"
      aria-live="polite"
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
      <h3
        className="text-sm font-medium m-0"
        style={{ color: "var(--fg)" }}
      >
        {title}
      </h3>
      {description && (
        <div className="text-xs mt-1 max-w-md">{description}</div>
      )}
      {children && <div className="mt-3">{children}</div>}
    </div>
  );
}
