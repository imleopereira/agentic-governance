"use client";

export function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    PASS: "bg-green-900/50 text-green-400 border-green-800",
    WARN: "bg-yellow-900/50 text-yellow-400 border-yellow-800",
    FAIL: "bg-red-900/50 text-red-400 border-red-800",
  };
  const cls = colors[status] ?? "bg-gray-900/50 text-gray-400 border-gray-800";
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium border ${cls}`}
    >
      {status}
    </span>
  );
}
