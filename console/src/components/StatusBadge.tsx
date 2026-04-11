"use client";

export function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, { bg: string; color: string; border: string; dot: string }> = {
    PASS: {
      bg: "rgba(34, 197, 94, 0.12)",
      color: "var(--success)",
      border: "rgba(34, 197, 94, 0.25)",
      dot: "var(--success)",
    },
    WARN: {
      bg: "rgba(234, 179, 8, 0.12)",
      color: "var(--warn)",
      border: "rgba(234, 179, 8, 0.25)",
      dot: "var(--warn)",
    },
    FAIL: {
      bg: "rgba(239, 68, 68, 0.12)",
      color: "var(--danger)",
      border: "rgba(239, 68, 68, 0.25)",
      dot: "var(--danger)",
    },
  };
  const s = styles[status] ?? {
    bg: "rgba(255, 255, 255, 0.06)",
    color: "var(--text-tertiary)",
    border: "var(--border)",
    dot: "var(--text-tertiary)",
  };
  return (
    <span
      className="inline-flex items-center gap-1.5 px-2 py-0.5 text-xs font-medium border"
      style={{
        background: s.bg,
        color: s.color,
        borderColor: s.border,
        borderRadius: "var(--radius-sm)",
      }}
    >
      <span
        className="inline-block w-1.5 h-1.5 rounded-full flex-shrink-0"
        style={{ background: s.dot }}
      />
      {status}
    </span>
  );
}
