/**
 * Zustand store for real-time SSE state.
 * REST data stays in TanStack Query; SSE state lives here.
 */

import { create } from "zustand";

import { api } from "@/lib/api";

export interface StreamEvent {
  event_id: string;
  session_id?: string;
  agent_id: string;
  kind: string;
  model?: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  chain_seq?: number;
  hmac_value?: string | null;
  prev_hash?: string | null;
  /** True if this row came from the SSE stream and still needs the full
   *  audit-event fields (model/metadata/hmac_value/prev_hash) hydrated
   *  via `api.getAuditEvent(event_id)`. Cleared once hydration resolves. */
  _needs_hydration?: boolean;
  /** monotonic time when received (performance.now()) */
  _received_at: number;
  /** stable React key */
  _local_id: string;
}

export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "disconnected"
  | "polling";

interface EventStreamStore {
  events: StreamEvent[];
  connectionStatus: ConnectionStatus;
  lastEventId: string | null;
  /** seconds until next reconnect attempt */
  reconnectIn: number;
  /** HITL pending count for sidebar badge */
  pendingApprovals: number;

  addEvents: (events: Omit<StreamEvent, "_local_id">[]) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
  setLastEventId: (id: string | null) => void;
  setReconnectIn: (seconds: number) => void;
  setPendingApprovals: (count: number) => void;
  clearEvents: () => void;
  /** Lazy-hydrate a stream event with its full audit-event fields. No-op if
   *  the event is already hydrated or not present. Safe to call repeatedly;
   *  hydration in-flight is deduped via the `_needs_hydration` flag. */
  hydrateEvent: (eventId: string) => Promise<void>;
}

const MAX_EVENTS = 2000;
let _counter = 0;

export const useEventStreamStore = create<EventStreamStore>((set) => ({
  events: [],
  connectionStatus: "connecting",
  lastEventId: null,
  reconnectIn: 0,
  pendingApprovals: 0,

  addEvents: (newEvents) =>
    set((state) => ({
      events: [
        ...newEvents.map((e) => ({ ...e, _local_id: String(++_counter) })),
        ...state.events,
      ].slice(0, MAX_EVENTS),
    })),

  setConnectionStatus: (connectionStatus) => set({ connectionStatus }),
  setLastEventId: (lastEventId) => set({ lastEventId }),
  setReconnectIn: (reconnectIn) => set({ reconnectIn }),
  setPendingApprovals: (pendingApprovals) => set({ pendingApprovals }),
  clearEvents: () => set({ events: [] }),

  hydrateEvent: async (eventId) => {
    const existing = useEventStreamStore
      .getState()
      .events.find((e) => e.event_id === eventId);
    if (!existing || !existing._needs_hydration) return;
    try {
      const full = await api.getAuditEvent(eventId);
      set((state) => ({
        events: state.events.map((e) =>
          e.event_id === eventId
            ? {
                ...e,
                model: full.model ?? e.model,
                metadata: full.metadata ?? e.metadata,
                hmac_value: full.hmac_value ?? e.hmac_value,
                prev_hash: full.prev_hash ?? e.prev_hash,
                chain_seq: full.chain_seq ?? e.chain_seq,
                _needs_hydration: false,
              }
            : e
        ),
      }));
    } catch {
      /* leave _needs_hydration true; consumer can retry */
    }
  },
}));
