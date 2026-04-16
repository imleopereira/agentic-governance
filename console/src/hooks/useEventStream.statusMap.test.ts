/**
 * useEventStream — agent bucketing rule (runtime).
 *
 * v0.6.1 polish sprint: replaces the v0.6 source-grep test that
 * read `useEventStream.ts` off disk and regex-matched the literal
 * bucketing expression plus the `_needs_hydration: true` marker.
 *
 * The runtime test below mounts the hook in jsdom, installs a fake
 * EventSource, emits three NOTIFY-shaped envelopes (real agent_id,
 * truncated-with-no-agent_id, missing-agent_id) and asserts the
 * buckets they land in inside the Zustand store.
 *
 * The `_needs_hydration` flag is asserted on the stored event, which
 * is the observable consequence of the old source-pin.
 */
import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
  vi,
} from "vitest";
import { renderHook, act } from "@testing-library/react";

import { useEventStream } from "./useEventStream";
import { useEventStreamStore } from "@/lib/store";

interface FakeES {
  url: string;
  withCredentials: boolean;
  onopen: ((this: FakeES, ev: Event) => void) | null;
  onmessage: ((this: FakeES, ev: MessageEvent) => void) | null;
  onerror: ((this: FakeES, ev: Event) => void) | null;
  listeners: Record<string, EventListener[]>;
  addEventListener: (type: string, listener: EventListener) => void;
  removeEventListener: (type: string, listener: EventListener) => void;
  close: () => void;
  _emitOpen: () => void;
  _emitMessage: (payload: unknown) => void;
}

let esInstances: FakeES[] = [];

function installFakeEventSource(): void {
  class FakeEventSource implements FakeES {
    url: string;
    withCredentials: boolean;
    onopen: ((this: FakeES, ev: Event) => void) | null = null;
    onmessage: ((this: FakeES, ev: MessageEvent) => void) | null = null;
    onerror: ((this: FakeES, ev: Event) => void) | null = null;
    listeners: Record<string, EventListener[]> = {};

    constructor(url: string, init?: { withCredentials?: boolean }) {
      this.url = url;
      this.withCredentials = Boolean(init?.withCredentials);
      esInstances.push(this as unknown as FakeES);
    }

    addEventListener(type: string, listener: EventListener): void {
      (this.listeners[type] ??= []).push(listener);
    }

    removeEventListener(type: string, listener: EventListener): void {
      this.listeners[type] = (this.listeners[type] ?? []).filter(
        (l) => l !== listener,
      );
    }

    close(): void {}

    _emitOpen(): void {
      this.onopen?.call(this as unknown as FakeES, new Event("open"));
    }

    _emitMessage(payload: unknown): void {
      const ev = new MessageEvent("message", {
        data: JSON.stringify(payload),
      });
      this.onmessage?.call(this as unknown as FakeES, ev);
    }
  }
  (globalThis as unknown as { EventSource: typeof FakeEventSource }).EventSource =
    FakeEventSource;
}

const baseEnvelope = (overrides: Record<string, unknown>) => ({
  event_id: "evt-1",
  session_id: "sess-1",
  kind: "tool.call",
  chain_seq: 1,
  created_at: "2026-04-15T12:00:00.000Z",
  ...overrides,
});

describe("useEventStream — agent bucketing rule (runtime)", () => {
  beforeEach(() => {
    esInstances = [];
    installFakeEventSource();
    useEventStreamStore.setState({
      events: [],
      connectionStatus: "connecting",
      lastEventId: null,
      reconnectIn: 0,
      pendingApprovals: 0,
    });
    // Silence the "Dropped invalid SSE envelope" warn in tests.
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function emitAndGetStored(payload: unknown): string {
    renderHook(() => useEventStream());
    act(() => {
      esInstances[0]._emitOpen();
    });
    act(() => {
      esInstances[0]._emitMessage(payload);
    });
    const events = useEventStreamStore.getState().events;
    expect(events.length).toBeGreaterThan(0);
    return events[0].agent_id;
  }

  it("routes real agent_id to that bucket", () => {
    const bucket = emitAndGetStored(
      baseEnvelope({ agent_id: "agent-a" }),
    );
    expect(bucket).toBe("agent-a");
  });

  it("routes truncated envelopes without agent_id to the _truncated bucket", () => {
    const bucket = emitAndGetStored(
      baseEnvelope({ truncated: true }),
    );
    expect(bucket).toBe("_truncated");
  });

  it("routes non-truncated envelopes without agent_id to the unknown bucket", () => {
    const bucket = emitAndGetStored(baseEnvelope({}));
    expect(bucket).toBe("unknown");
  });

  it("routes null agent_id + truncated=true to _truncated", () => {
    const bucket = emitAndGetStored(
      baseEnvelope({ agent_id: null, truncated: true }),
    );
    expect(bucket).toBe("_truncated");
  });

  it("sets _needs_hydration:true on every stream-sourced event", () => {
    renderHook(() => useEventStream());
    act(() => esInstances[0]._emitOpen());
    act(() =>
      esInstances[0]._emitMessage(
        baseEnvelope({ agent_id: "agent-a" }),
      ),
    );
    const stored = useEventStreamStore.getState().events[0];
    expect(stored._needs_hydration).toBe(true);
  });
});
