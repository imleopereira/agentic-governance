"use client";
/**
 * SSE client hook with auto-reconnect and Last-Event-ID replay.
 * Feeds all events into the Zustand store.
 * Mount once in the authenticated shell; pages just read the store.
 */

import { useEffect, useRef } from "react";
import { useEventStreamStore } from "@/lib/store";
import { validateSseEnvelope } from "@/lib/sseValidator";
import { validateMetadata } from "@/lib/metadataValidator";

// DA Wave 4 SHIP-WITH-CHANGES (F8): wire the hand-rolled validators
// into the live SSE path. Malformed envelopes are dropped, metadata
// fields that fail the narrowing rules are coerced to `null`, and a
// single `console.warn` per drop gives operators a drift signal. See
// `sseValidator.ts` / `metadataValidator.ts` for rationale.

/** Monotonic counter of dropped SSE envelopes, exported for tests and
 *  for any future operator telemetry sidecar. Lives on the module so a
 *  consumer can read it without pulling in the Zustand store. */
export let droppedEnvelopeCount = 0;
/** Test-only reset. Not part of the public API. */
export function __resetDroppedEnvelopeCount(): void {
  droppedEnvelopeCount = 0;
}

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
        let raw: unknown;
        try {
          raw = JSON.parse(e.data as string);
        } catch {
          droppedEnvelopeCount += 1;
          console.warn("Dropped invalid SSE envelope", { reason: "json_parse" });
          return;
        }
        // DA Wave 4 F8 wiring: run the strict validator BEFORE touching
        // the store. A truncated fallback envelope is still legal — the
        // validator accepts `truncated: true` paired with a bounded
        // chain_seq. Anything else drops to a single-line warn.
        //
        // Back-compat bucketing (F2): when the NOTIFY trigger sheds
        // fields over the 7 KB cap it emits `{truncated: true}` with
        // possibly null agent_id. Preserve the old
        //     raw.agent_id ?? (raw.truncated ? "_truncated" : "unknown")
        // bucketing rule by rewriting a missing agent_id to a synthetic
        // value BEFORE revalidation, so the strict validator still
        // passes and the TrailPanel "by agent" filter keeps working.
        const rawAny = raw as Record<string, unknown>;
        if (
          typeof rawAny.agent_id !== "string" ||
          rawAny.agent_id.length === 0
        ) {
          rawAny.agent_id = rawAny.truncated ? "_truncated" : "unknown";
        }
        const envelope = validateSseEnvelope(raw);
        if (envelope === null) {
          droppedEnvelopeCount += 1;
          console.warn("Dropped invalid SSE envelope", {
            reason: "schema_mismatch",
          });
          return;
        }
        // Metadata narrowing: the NOTIFY envelope does not carry metadata
        // but a (future) richer payload might. Run the validator now so
        // any non-scalar leak is coerced to null at the store boundary.
        const rawObj = raw as Record<string, unknown>;
        const rawMeta =
          rawObj.metadata && typeof rawObj.metadata === "object"
            ? (rawObj.metadata as Record<string, unknown>)
            : null;
        const meta = validateMetadata(rawMeta);
        useEventStreamStore.getState().addEvents([
          {
            event_id: envelope.event_id,
            session_id: envelope.session_id ?? undefined,
            agent_id: envelope.agent_id,
            kind: kindOverride ?? envelope.kind,
            model: meta.model,
            metadata: {
              model: meta.model,
              tool: meta.tool,
              request_id: meta.request_id,
            },
            created_at: envelope.created_at,
            chain_seq: envelope.chain_seq,
            _needs_hydration: true,
            _received_at: performance.now(),
          },
        ]);
        if (e.lastEventId) {
          useEventStreamStore.getState().setLastEventId(e.lastEventId);
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
