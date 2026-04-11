/**
 * Zustand store for real-time SSE state.
 * REST data stays in TanStack Query; SSE state lives here.
 */

import { create } from "zustand";

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
}));
