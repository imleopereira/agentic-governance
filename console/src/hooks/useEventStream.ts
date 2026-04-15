"use client";
/**
 * SSE client hook with auto-reconnect and Last-Event-ID replay.
 * Feeds all events into the Zustand store.
 * Mount once in the authenticated shell; pages just read the store.
 */

import { useEffect, useRef } from "react";
import { useEventStreamStore } from "@/lib/store";

export function useEventStream() {
  const esRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const countdownTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectDelayRef = useRef(3); // seconds, doubles on each failure up to 30s
  const mountedRef = useRef(true);
  const connectRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    mountedRef.current = true;

    function scheduleReconnect() {
      if (!mountedRef.current) return;

      if (countdownTimerRef.current) clearInterval(countdownTimerRef.current);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);

      let remaining = reconnectDelayRef.current;
      useEventStreamStore.getState().setReconnectIn(remaining);

      countdownTimerRef.current = setInterval(() => {
        remaining -= 1;
        useEventStreamStore.getState().setReconnectIn(Math.max(0, remaining));
        if (remaining <= 0 && countdownTimerRef.current) {
          clearInterval(countdownTimerRef.current);
          countdownTimerRef.current = null;
        }
      }, 1000);

      reconnectTimerRef.current = setTimeout(() => {
        reconnectDelayRef.current = Math.min(reconnectDelayRef.current * 2, 30);
        connectRef.current?.();
      }, reconnectDelayRef.current * 1000);
    }

    function connect() {
      if (!mountedRef.current) return;

      const store = useEventStreamStore.getState();
      store.setConnectionStatus("connecting");

      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }

      const lastId = useEventStreamStore.getState().lastEventId;
      const qs = lastId ? `?last_event_id=${encodeURIComponent(lastId)}` : "";
      const url = `/api/stream/events${qs}`;

      let es: EventSource;
      try {
        es = new EventSource(url, { withCredentials: true });
      } catch {
        scheduleReconnect();
        return;
      }

      esRef.current = es;

      es.onopen = () => {
        if (!mountedRef.current) return;
        useEventStreamStore.getState().setConnectionStatus("connected");
        useEventStreamStore.getState().setReconnectIn(0);
        reconnectDelayRef.current = 3;
      };

      function handleMessage(e: MessageEvent, kindOverride?: string) {
        if (!mountedRef.current) return;
        try {
          const raw = JSON.parse(e.data as string) as Record<string, unknown>;
          useEventStreamStore.getState().addEvents([
            {
              // The backend NOTIFY trigger emits a minimal envelope —
              // event_id, session_id, agent_id, kind, created_at, chain_seq,
              // optional `truncated` — to keep WAL payload small. Fields
              // like model/metadata/hmac_value/prev_hash are NOT in the
              // stream; they are lazy-hydrated via api.getAuditEvent on
              // first access (see store / EventDetailDrawer).
              event_id: String(raw.event_id ?? crypto.randomUUID()),
              session_id: raw.session_id as string | undefined,
              agent_id: String(
                raw.agent_id ?? (raw.truncated ? "_truncated" : "unknown")
              ),
              kind: kindOverride ?? String(raw.kind ?? "audit"),
              metadata: {},
              created_at: String(
                raw.created_at ?? new Date().toISOString()
              ),
              chain_seq: raw.chain_seq as number | undefined,
              _needs_hydration: true,
              _received_at: performance.now(),
            },
          ]);
          if (e.lastEventId) {
            useEventStreamStore.getState().setLastEventId(e.lastEventId);
          }
        } catch {
          /* ignore parse errors */
        }
      }

      es.onmessage = (e) => handleMessage(e);

      for (const kind of [
        "audit",
        "presence",
        "gate",
        "budget",
        "scope",
        "system",
        "loop",
      ]) {
        es.addEventListener(kind, (e: Event) =>
          handleMessage(e as MessageEvent, kind)
        );
      }

      es.onerror = () => {
        if (!mountedRef.current) return;
        es.close();
        esRef.current = null;
        useEventStreamStore.getState().setConnectionStatus("disconnected");
        scheduleReconnect();
      };
    }

    connectRef.current = connect;
    connect();

    return () => {
      mountedRef.current = false;
      esRef.current?.close();
      esRef.current = null;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (countdownTimerRef.current) clearInterval(countdownTimerRef.current);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function manualReconnect() {
    if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
    if (countdownTimerRef.current) clearInterval(countdownTimerRef.current);
    reconnectDelayRef.current = 3;
    useEventStreamStore.getState().setReconnectIn(0);
    connectRef.current?.();
  }

  return { manualReconnect };
}
