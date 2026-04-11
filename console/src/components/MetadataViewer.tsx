"use client";

import { useState } from "react";

/** Render JSON with lightweight syntax coloring for keys vs values. */
function SyntaxJson({ data }: { data: Record<string, unknown> }) {
  const json = JSON.stringify(data, null, 2);
  // Split into tokens: keys (quoted strings before colons), strings, numbers, booleans, null
  const parts = json.split(
    /("(?:[^"\\]|\\.)*"\s*:)|("(?:[^"\\]|\\.)*")|(\b(?:true|false|null)\b)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g
  );

  return (
    <code>
      {parts.map((part, i) => {
        if (part === undefined || part === "") return null;
        // Key (quoted string followed by colon)
        if (/^".*":\s*$/.test(part))
          return (
            <span key={i} style={{ color: "#93C5FD" }}>
              {part}
            </span>
          );
        // String value
        if (/^".*"$/.test(part))
          return (
            <span key={i} style={{ color: "#86EFAC" }}>
              {part}
            </span>
          );
        // Boolean / null
        if (/^(true|false|null)$/.test(part))
          return (
            <span key={i} style={{ color: "#FDBA74" }}>
              {part}
            </span>
          );
        // Number
        if (/^-?\d/.test(part))
          return (
            <span key={i} style={{ color: "#FDE68A" }}>
              {part}
            </span>
          );
        return <span key={i}>{part}</span>;
      })}
    </code>
  );
}

export function MetadataViewer({ data }: { data: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const keys = Object.keys(data);

  if (keys.length === 0) {
    return <span style={{ color: "var(--text-tertiary)" }}>-</span>;
  }

  async function copyJson() {
    const text = JSON.stringify(data, null, 2);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
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
        onClick={(e) => {
          e.stopPropagation();
          setExpanded(true);
        }}
        className="text-left font-mono text-xs transition-colors hover:text-[var(--fg)]"
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
    <div
      className="relative animate-fade-in-up"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="absolute top-1.5 right-1.5 flex items-center gap-1.5 z-10">
        <button
          onClick={copyJson}
          className="text-[10px] px-1.5 py-0.5 border transition-colors"
          style={{
            borderColor: copied ? "var(--accent)" : "var(--border)",
            color: copied ? "var(--accent)" : "var(--text-tertiary)",
            borderRadius: "var(--radius-sm)",
            background: "var(--surface)",
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
        <button
          onClick={() => setExpanded(false)}
          className="text-[10px] px-1.5 py-0.5 transition-colors"
          style={{ color: "var(--text-tertiary)" }}
        >
          collapse
        </button>
      </div>
      <pre
        className="font-mono text-xs p-3 pt-7 max-h-40 overflow-auto whitespace-pre-wrap"
        style={{
          background: "rgba(0, 0, 0, 0.3)",
          borderRadius: "var(--radius-sm)",
        }}
      >
        <SyntaxJson data={data} />
      </pre>
    </div>
  );
}
