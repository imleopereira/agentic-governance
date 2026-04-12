"use client";

import type { ReactNode } from "react";

export interface SectionLabelProps {
  children: ReactNode;
  /** Optional right-aligned adornment (e.g. a count badge). */
  adornment?: ReactNode;
}

/** Small-caps section header used inside drill sub-panels and trace cards. */
export function SectionLabel({ children, adornment }: SectionLabelProps) {
  return (
    <div className="flex items-center justify-between mb-2">
      <h3
        className="text-[11px] font-semibold uppercase tracking-widest"
        style={{ color: "var(--text-tertiary)" }}
      >
        {children}
      </h3>
      {adornment && <div>{adornment}</div>}
    </div>
  );
}
