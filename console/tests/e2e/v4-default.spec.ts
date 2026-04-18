// Smoke spec for the v0.6.2 v4-default flip. Three intended checks:
//   1. `/` serves the v4 shell (first-impression for design partners)
//   2. Compliance nav link is wired + export button visible
//   3. `?ui=v3` query param sets the console_ui_version cookie
//
// Known gap — these tests currently skip. They hit the authenticated
// console routes (/agents, /compliance) and we have no test-session
// scaffolding yet. Local dogfood run post-consolidation confirmed:
//   - Tests #1/#2 redirect into the login page, not /agents
//   - Test #3 never triggers middleware because `?ui=v3` lands on the
//     login page where middleware cookie-setting is not exercised
//
// Fix scope for v0.7: add a `test.beforeEach` that authenticates via
// the `GOVERNANCE_CONSOLE_DEV_MODE` path or a seeded session cookie.
// Once that lands, remove the `.skip()` below. CI already has
// `continue-on-error: true` on `console-e2e-smoke` so this is not a
// merge blocker.
//
// Selectors favour accessible roles (getByRole) over CSS classes so
// restyles don't re-break this gate.

import { test, expect } from '@playwright/test';

test.describe.skip('v4 is the default console UI — requires v0.7 test-auth scaffolding', () => {
  test('/ redirects to /agents under v4', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/agents/);
    // Presence of a top-level heading is a soft v4 fingerprint —
    // stronger than route-only because the v3 landing was a topology
    // graph without an H1 at the route root.
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  });

  test('Compliance nav link is present and navigates', async ({ page }) => {
    await page.goto('/agents');

    // Agent E ships the Compliance link in the v4 sidebar. We match by
    // accessible name so minor copy changes ("Compliance" vs
    // "Compliance center") still resolve.
    const complianceLink = page.getByRole('link', { name: /compliance/i });
    await expect(complianceLink).toBeVisible();
    await complianceLink.click();

    await expect(page).toHaveURL(/\/compliance/);

    // Wave 1 Agent D shipped the Article 12 export button; Agent G is
    // instructed to keep the label stable at "Export Article 12
    // evidence" (case-insensitive match here for resilience).
    await expect(
      page.getByRole('button', { name: /export article 12 evidence/i }),
    ).toBeVisible();
  });

  test('?ui=v3 query param reverts to v3 and sets cookie', async ({
    page,
    context,
  }) => {
    await page.goto('/?ui=v3');

    const cookies = await context.cookies();
    const uiCookie = cookies.find((c) => c.name === 'console_ui_version');
    expect(uiCookie?.value).toBe('v3');

    // Re-visiting without the query string should respect the cookie
    // and keep the user on v3. We only assert the cookie persists
    // because v3 and v4 both route under /agents at the top level; a
    // DOM-level distinguisher ("Topology" heading vs v4 dashboard
    // heading) is fragile across the E/F/G merge window.
    //
    // TODO(v0.6.2): once v4 ships a stable v4-only sentinel (e.g. a
    // data-testid="v4-shell" on the layout root), add a DOM assert here
    // that we are NOT on v4.
    await page.goto('/');
    const cookiesAfter = await context.cookies();
    expect(
      cookiesAfter.find((c) => c.name === 'console_ui_version')?.value,
    ).toBe('v3');
  });
});
