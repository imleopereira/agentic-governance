import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for the Code Atelier Governance console.
 *
 * Purpose (v0.6.2): CTO flagged that flipping v4 to the default
 * console UI without any E2E coverage is a ship blocker. This config
 * runs a tiny smoke suite (tests/e2e/) in Chromium only — v0.6.2 is a
 * polish release, not a cross-browser milestone. Firefox / WebKit can
 * be added when the console grows past "one hero flow".
 */
export default defineConfig({
  testDir: './tests/e2e',
  // 30s per test. Smoke flows are fast; a test exceeding this is a
  // regression, not a reason to raise the budget.
  timeout: 30_000,
  // Retry on CI only — local runs should surface flakes, not mask them.
  retries: process.env.CI ? 2 : 0,
  // Single worker on CI keeps the shared Next dev server predictable.
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [['github'], ['list']] : 'list',
  use: {
    baseURL: 'http://localhost:3000',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: 'npm run dev',
    port: 3000,
    // Locally, reuse an already-running dev server so contributors can
    // iterate on specs without restarting Next. On CI, always boot a
    // fresh one to avoid stale-build false positives.
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
