"use client";

export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      className={`animate-pulse ${className}`}
      style={{
        background: "rgba(255, 255, 255, 0.04)",
        borderRadius: "var(--radius-sm)",
      }}
    />
  );
}

export function CardSkeleton() {
  return (
    <div
      className="border p-4 space-y-3"
      style={{
        borderColor: "var(--border)",
        background: "var(--card)",
        borderRadius: "var(--radius-md)",
      }}
    >
      <div className="flex justify-between">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-4 w-16" />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Skeleton className="h-8" />
        <Skeleton className="h-8" />
        <Skeleton className="h-8" />
        <Skeleton className="h-8" />
      </div>
      <Skeleton className="h-3 w-48" />
    </div>
  );
}

export function TableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-10 w-full" />
      ))}
    </div>
  );
}
