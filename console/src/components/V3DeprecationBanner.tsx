/**
 * V3DeprecationBanner — F7 sub-item #8.
 *
 * Gated by `NEXT_PUBLIC_SHOW_V3_BANNER`, which defaults to OFF.
 *
 * Why the gate (v0.6 team review): the banner has no telemetry
 * pipeline yet, and shipping with no instrumentation against the
 * current default-install customer base (zero) was creating noise in
 * the default v3 experience without any way to measure whether
 * operators actually saw, dismissed, or clicked through it. The DA +
 * DX review flagged this as a v0.6-tag blocker. The fix is to gate
 * the banner behind an explicit env var so Leo can enable it locally
 * during preview but default-install customers do not see it.
 *
 * The mount in `console/src/app/(v3)/layout.tsx` stays put — when the
 * gate is off, the component returns `null` and renders nothing.
 *
 * Shows a banner at the top of the v3 console advising operators that
 * a v4 opt-in is available and that the default flips in v0.7. The
 * banner is rendered on every v3 page via the root layout.
 *
 * Visibility (when the gate is on):
 * - shown when `process.env.NEXT_PUBLIC_CONSOLE_UI_VERSION !== "v4"`
 * - i.e. the default (unset) case AND explicit v3 show the banner
 * - only `NEXT_PUBLIC_CONSOLE_UI_VERSION=v4` hides it
 *
 * Telemetry:
 *   The original F7 scope called for 5 telemetry events (banner_shown,
 *   banner_dismissed, banner_v4_clicked, etc). Telemetry infra is out
 *   of scope for v0.6 — no event pipeline, no analytics destination.
 *   TODO(v0.7.0): wire 5 telemetry events once telemetry infra lands,
 *   then revisit whether the NEXT_PUBLIC_SHOW_V3_BANNER gate should
 *   default to on.
 *   Tracked as part of the v0.7 roadmap (see project_5_version_roadmap).
 *
 * Server component on purpose: no interactivity, reads env at build
 * time, zero client-side JS. Note: NEXT_PUBLIC_SHOW_V3_BANNER is
 * baked at `next build` time like every other NEXT_PUBLIC_* var —
 * see docs/configuration.md for the build-time caveat.
 */

export function V3DeprecationBanner() {
  if (process.env.NEXT_PUBLIC_SHOW_V3_BANNER !== "1") {
    return null;
  }
  const uiVersion = process.env.NEXT_PUBLIC_CONSOLE_UI_VERSION;
  if (uiVersion === "v4") {
    return null;
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="w-full border-b border-amber-700/40 bg-amber-950/40 px-4 py-2 text-center text-xs text-amber-200"
    >
      You are viewing console v3. v4 is opt-in via
      {" "}
      <code className="rounded bg-amber-900/40 px-1 py-0.5 font-mono">
        NEXT_PUBLIC_CONSOLE_UI_VERSION=v4
      </code>
      . Default flips in v0.7.
    </div>
  );
}
