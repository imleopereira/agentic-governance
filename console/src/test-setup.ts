/**
 * Global test setup for vitest runs.
 *
 * Loaded before every test file via `vitest.config.ts :: setupFiles`.
 * Wires jest-dom matchers and installs browser-API stubs that a plain
 * jsdom environment does not ship but that our components rely on.
 *
 * Why this file exists (v0.6.1 polish sprint): replacing the 8 LLM-
 * theater source-grep tests with runtime render+assert tests requires
 * a real DOM environment. The old Node-only vitest run could not mount
 * React at all; this setup is what unlocks that.
 */
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// React Testing Library leaves rendered trees attached to document.body
// between tests when `globals: false`. Clean up explicitly so tests
// don't leak DOM across the file.
afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// ResizeObserver — not implemented by jsdom. Several Next.js / headless-UI
// primitives instantiate one on mount. A no-op shim is sufficient for the
// runtime tests added in v0.6.1.
// ---------------------------------------------------------------------------
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis.ResizeObserver === "undefined") {
  (globalThis as unknown as { ResizeObserver: typeof ResizeObserverStub }).ResizeObserver =
    ResizeObserverStub;
}

// ---------------------------------------------------------------------------
// matchMedia — jsdom returns `undefined`. ComplianceHeaderPill calls
// `window.matchMedia("(prefers-reduced-motion: reduce)")` inside its
// render path; the stub returns a MediaQueryList-shaped object with
// `matches: false` so the non-reduced-motion branch renders.
// ---------------------------------------------------------------------------
if (typeof window !== "undefined" && typeof window.matchMedia === "undefined") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  });
}
