"use client";

export function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, { bg: string; color: string; border: string }> = {
    PASS: {
      bg: "rgba(34, 197, 94, 0.08)",
      color: "var(--success)",
      border: "rgba(34, 197, 94, 0.3)",
    },
    WARN: {
      bg: "rgba(234, 179, 8, 0.08)",
      color: "var(--warn)",
      border: "rgba(234, 179, 8, 0.3)",
    },
    FAIL: {
      bg: "rgba(239, 68, 68, 0.08)",
      color: "var(--danger)",
      border: "rgba(239, 68, 68, 0.3)",
    },
  };
  const s = styles[status] ?? {
    bg: "rgba(255, 255, 255, 0.04)",
    color: "var(--text-tertiary)",
    border: "var(--border)",
  };
  return (
    <span
      className="inline-flex items-center px-2 py-0.5 text-xs font-medium border"
      style={{
        background: s.bg,
        color: s.color,
        borderColor: s.border,
        borderRadius: "var(--radius-sm)",
      }}
    >
      {status}
    </span>
  );
}
