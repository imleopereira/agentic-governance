/**
 * useEventStream — reconnect-scheduling contract (runtime).
 *
 * v0.6.1 polish sprint: replaces the v0.6 source-grep test that
 * read `useEventStream.ts` off disk and regex-matched
 * `reconnectDelayRef = useRef(3)`, the backoff cap literal, and the
 * `setConnectionStatus("connecting" / "connected" / "disconnected")`
 * call-sites.
 *
 * The runtime test below mounts the hook in a jsdom environment,
 * installs a fake `EventSource` that we can drive via `emitOpen()` /
 * `emitError()`, advances vitest fake timers, and asserts the store
 * transitions + the URL the hook opens (for Last-Event-ID replay).
 *
 * Why it's a `.ts` file (no JSX): `renderHook` mounts React without
 * any custom tree, so we don't need the JSX transform here.
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

// ---------------------------------------------------------------------------
// Fake EventSource — captures every instance the hook creates so the test
// can drive `open` / `error` / `message` events and inspect the URL the
// hook asked for (for Last-Event-ID replay verification).
// ---------------------------------------------------------------------------
interface FakeES {
  url: string;
  withCredentials: boolean;
  readyState: number;
  onopen: ((this: FakeES, ev: Event) => void) | null;
  onmessage: ((this: FakeES, ev: MessageEvent) => void) | null;
  onerror: ((this: FakeES, ev: Event) => void) | null;
  addEventListener: (type: string, listener: EventListener) => void;
  removeEventListener: (type: string, listener: EventListener) => void;
  close: () => void;
  _emitOpen: () => void;
  _emitError: () => void;
}

let esInstances: FakeES[] = [];

function installFakeEventSource(): void {
  class FakeEventSource implements FakeES {
    url: string;
    withCredentials: boolean;
    readyState = 0;
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

    close(): void {
      this.readyState = 2;
    }

    _emitOpen(): void {
      this.readyState = 1;
      this.onopen?.call(this as unknown as FakeES, new Event("open"));
    }

    _emitError(): void {
      this.onerror?.call(this as unknown as FakeES, new Event("error"));
    }
  }
  (globalThis as unknown as { EventSource: typeof FakeEventSource }).EventSource =
    FakeEventSource;
}

describe("useEventStream — reconnect backoff + store transitions (runtime)", () => {
  beforeEach(() => {
    esInstances = [];
    installFakeEventSource();
    vi.useFakeTimers();
    // Reset store state between tests.
    useEventStreamStore.setState({
      events: [],
      connectionStatus: "connecting",
      lastEventId: null,
      reconnectIn: 0,
      pendingApprovals: 0,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("transitions connecting → connected on open", async () => {
    renderHook(() => useEventStream());
    // Hook mounts synchronously and opens an EventSource.
    expect(esInstances).toHaveLength(1);
    expect(useEventStreamStore.getState().connectionStatus).toBe("connecting");
    act(() => {
      esInstances[0]._emitOpen();
    });
    expect(useEventStreamStore.getState().connectionStatus).toBe("connected");
  });

  it("transitions connected → disconnected on error and schedules reconnect", () => {
    renderHook(() => useEventStream());
    act(() => {
      esInstances[0]._emitOpen();
    });
    expect(useEventStreamStore.getState().connectionStatus).toBe("connected");
    act(() => {
      esInstances[0]._emitError();
    });
    expect(useEventStreamStore.getState().connectionStatus).toBe(
      "disconnected",
    );
    // Reconnect scheduled — reconnectIn should be the initial 3 seconds.
    expect(useEventStreamStore.getState().reconnectIn).toBe(3);
  });

  it("doubles the backoff 3 → 6 on the second failure", () => {
    renderHook(() => useEventStream());
    act(() => esInstances[0]._emitOpen());
    act(() => esInstances[0]._emitError());
    // Advance past the 3s timer to let scheduleReconnect fire.
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    // A new EventSource was created by the reconnect.
    expect(esInstances.length).toBeGreaterThanOrEqual(2);
    // Fail it again; reconnectIn should now report 6s.
    const latest = esInstances[esInstances.length - 1];
    act(() => latest._emitError());
    expect(useEventStreamStore.getState().reconnectIn).toBe(6);
  });

  it("resets the backoff to 3s after a successful open", () => {
    renderHook(() => useEventStream());
    // Fail once: backoff bumps to 6 internally.
    act(() => esInstances[0]._emitError());
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    // New attempt. Fail it too so the delay bumps to 12s internally.
    const second = esInstances[esInstances.length - 1];
    act(() => second._emitError());
    // Now advance and let the third attempt OPEN — the delay must reset.
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    const third = esInstances[esInstances.length - 1];
    act(() => third._emitOpen());
    // Fail the third connection — reconnectIn must be 3 again, proving reset.
    act(() => third._emitError());
    expect(useEventStreamStore.getState().reconnectIn).toBe(3);
  });

  it("caps the backoff at 30 seconds no matter how many failures compound", () => {
    renderHook(() => useEventStream());
    // Fail, advance past delay, repeat 8 times — the cap should kick in.
    const delays = [3000, 6000, 12000, 24000, 30000, 30000, 30000, 30000];
    for (const delay of delays) {
      const latest = esInstances[esInstances.length - 1];
      act(() => latest._emitError());
      act(() => {
        vi.advanceTimersByTime(delay);
      });
    }
    // Fail once more; reconnectIn must be <= 30.
    const latest = esInstances[esInstances.length - 1];
    act(() => latest._emitError());
    expect(useEventStreamStore.getState().reconnectIn).toBeLessThanOrEqual(30);
  });

  it("uses Last-Event-ID replay on reconnect when a lastEventId has been set", () => {
    useEventStreamStore.setState({ lastEventId: "evt-last-99" });
    renderHook(() => useEventStream());
    const url = esInstances[0].url;
    expect(url).toContain("last_event_id=evt-last-99");
  });

  it("does NOT include last_event_id on the first connect with empty state", () => {
    renderHook(() => useEventStream());
    expect(esInstances[0].url).not.toContain("last_event_id=");
  });
});
