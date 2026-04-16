// This spec requires Wave 1.5 E + F + G merged. Expected to pass after
// consolidation. On release/v0.6 alone it will fail because v4 is not
// yet the default and the Compliance sidebar link / export button
// landed in sibling worktrees.
//
// Scope of this smoke (intentionally tiny — v0.6.2 is polish):
//   1. `/` redirects into the v4 shell (first impression for design
//      partners)
//   2. Compliance nav link is wired in the sidebar AND lands on the
//      compliance page with the Article 12 export button visible
//   3. `?ui=v3` escape hatch still flips the cookie and reverts the UI
//
// Selectors favour accessible roles (getByRole) over CSS classes so
// Agents E/F/G can restyle freely without breaking this gate.

import { test, expect } from '@playwright/test';

test.describe('v4 is the default console UI', () => {
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
