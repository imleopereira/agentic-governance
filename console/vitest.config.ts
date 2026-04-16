/**
 * Vitest configuration for the v4 governance console.
 *
 * v0.6.1 polish sprint: the console now runs tests in a jsdom
 * environment so runtime render + assert patterns replace the
 * v0.6-era source-grep tests ("LLM-theater pattern"). See the
 * CI tripwire in `.github/workflows/test.yml` — the count of
 * `readFileSync` occurrences in console test files is asserted
 * to be zero on every push.
 *
 * The config is intentionally minimal:
 *   - jsdom environment — React components can render, fire events,
 *     and be queried via @testing-library/react.
 *   - setup file — wires @testing-library/jest-dom matchers and
 *     installs a ResizeObserver + matchMedia stub so components
 *     that poke at window APIs on mount don't blow up.
 *   - @vitejs/plugin-react — .tsx JSX transform for tests.
 *   - resolve alias — keeps `@/` imports working (mirrors
 *     `tsconfig.json :: paths`).
 */
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
