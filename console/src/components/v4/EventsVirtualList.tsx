"use client";
/**
 * v4 drill-over Trail-tab wrapper around the existing VirtualList.
 *
 * Does NOT reimplement windowing — delegates to
 * `@/components/VirtualList` (read-only from v4's perspective).
 * Accepts an optional per-row renderer so the Trail tab can render
 * both collapsed and expanded states; otherwise falls back to a
 * minimal event row.
 */

import { useCallback, type ReactNode } from "react";

import { VirtualList } from "@/components/VirtualList";
import type { AuditEvent } from "@/lib/api";

export interface EventsVirtualListProps {
  events: AuditEvent[];
  onEventClick?: (event: AuditEvent) => void;
  renderItem?: (event: AuditEvent, expanded: boolean) => ReactNode;
  expandedId?: string | null;
  itemHeight?: number;
  className?: string;
}

export function EventsVirtualList({
  events,
  onEventClick,
  renderItem,
  expandedId = null,
  itemHeight,
  className,
}: EventsVirtualListProps) {
  const rowRenderer = useCallback(
    (event: AuditEvent) => {
      const expanded = expandedId === event.event_id;
      const content = renderItem ? (
        renderItem(event, expanded)
      ) : (
        <DefaultEventRow event={event} />
      );

      return (
        <div
          key={event.event_id}
          role={onEventClick ? "button" : undefined}
          tabIndex={onEventClick ? 0 : undefined}
          onClick={onEventClick ? () => onEventClick(event) : undefined}
          onKeyDown={
            onEventClick
              ? (e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onEventClick(event);
                  }
                }
              : undefined
          }
          style={{
            cursor: onEventClick ? "pointer" : "default",
          }}
        >
          {content}
        </div>
      );
    },
    [onEventClick, renderItem, expandedId]
  );

  return (
    <VirtualList<AuditEvent>
      items={events}
      itemHeight={itemHeight}
      className={className}
      renderItem={rowRenderer}
    />
  );
}

function DefaultEventRow({ event }: { event: AuditEvent }) {
  return (
    <div
      style={{
        padding: "0.625rem 0.875rem",
        borderBottom: "1px solid var(--border)",
        fontSize: "0.8125rem",
        display: "flex",
        gap: "0.75rem",
        alignItems: "center",
      }}
    >
      <span style={{ fontFamily: "var(--font-mono)", opacity: 0.7 }}>
        #{event.chain_seq}
      </span>
      <span style={{ fontWeight: 500 }}>{event.kind}</span>
      <span style={{ opacity: 0.6, marginLeft: "auto" }}>
        {event.created_at ?? ""}
      </span>
    </div>
  );
}
