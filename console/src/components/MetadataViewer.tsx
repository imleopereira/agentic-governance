"use client";

import { useState } from "react";

export function MetadataViewer({ data }: { data: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false);
  const keys = Object.keys(data);

  if (keys.length === 0) {
    return <span style={{ color: "var(--text-tertiary)" }}>&mdash;</span>;
  }

  if (!expanded) {
    const preview = keys
      .slice(0, 2)
      .map((k) => `${k}: ${JSON.stringify(data[k])}`)
      .join(", ");
    const truncated =
      preview.length > 60 ? preview.slice(0, 60) + "..." : preview;
    return (
      <button
        onClick={() => setExpanded(true)}
        className="text-left font-mono text-xs transition-colors"
        style={{ color: "var(--text-tertiary)" }}
        title="Click to expand"
      >
        {truncated}
        {keys.length > 2 && (
          <span className="ml-1" style={{ color: "var(--accent)" }}>
            +{keys.length - 2} more
          </span>
        )}
      </button>
    );
  }

  return (
    <div className="relative">
      <button
        onClick={() => setExpanded(false)}
        className="absolute top-1 right-1 text-xs"
        style={{ color: "var(--text-tertiary)" }}
      >
        collapse
      </button>
      <pre
        className="font-mono text-xs p-2 max-h-40 overflow-auto whitespace-pre-wrap"
        style={{
          background: "rgba(0, 0, 0, 0.3)",
          borderRadius: "var(--radius-sm)",
        }}
      >
        {JSON.stringify(data, null, 2)}
      </pre>
    </div>
  );
}
