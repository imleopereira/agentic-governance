"use client";

import { useState } from "react";

export function MetadataViewer({ data }: { data: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false);
  const keys = Object.keys(data);

  if (keys.length === 0) {
    return <span className="text-[var(--muted)]">—</span>;
  }

  if (!expanded) {
    const preview = keys.slice(0, 2).map((k) => `${k}: ${JSON.stringify(data[k])}`).join(", ");
    const truncated = preview.length > 60 ? preview.slice(0, 60) + "..." : preview;
    return (
      <button
        onClick={() => setExpanded(true)}
        className="text-left font-mono text-xs text-[var(--muted)] hover:text-[var(--fg)] transition"
        title="Click to expand"
      >
        {truncated}
        {keys.length > 2 && (
          <span className="ml-1 text-[var(--accent)]">+{keys.length - 2} more</span>
        )}
      </button>
    );
  }

  return (
    <div className="relative">
      <button
        onClick={() => setExpanded(false)}
        className="absolute top-1 right-1 text-xs text-[var(--muted)] hover:text-white"
      >
        collapse
      </button>
      <pre className="font-mono text-xs bg-black/30 rounded p-2 max-h-40 overflow-auto whitespace-pre-wrap">
        {JSON.stringify(data, null, 2)}
      </pre>
    </div>
  );
}
