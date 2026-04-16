/**
 * V3DeprecationBanner — F7 sub-item #8.
 *
 * Gated by `NEXT_PUBLIC_SHOW_V3_BANNER`, which defaults to OFF.
 *
 * v0.6.2 flip: v4 is now the default. This banner now targets operators
 * who have FORCED v3 (via `NEXT_PUBLIC_CONSOLE_UI_VERSION=v3` at build
 * time, or the `?ui=v3` cookie override), warning them that v3 will be
 * removed in v0.7. Default-install customers no longer see this banner
 * because they land on v4 out of the box.
 *
 * Why the gate (v0.6 team review): the banner has no telemetry
 * pipeline yet. Shipping without instrumentation would add noise to
 * the default experience without any way to measure whether operators
 * actually saw, dismissed, or clicked through it. The DA + DX review
 * flagged this as a v0.6-tag blocker. The fix is to gate the banner
 * behind an explicit env var so Leo can enable it locally during
 * preview but default-install customers do not see it.
 *
 * The mount in `console/src/app/(v3)/layout.tsx` stays put — when the
 * gate is off, the component returns `null` and renders nothing.
 *
 * Visibility (when the gate is on):
 * - shown ONLY when `process.env.NEXT_PUBLIC_CONSOLE_UI_VERSION === "v3"`
 * - i.e. the forced-v3 case (env var set explicitly at build time)
 * - default (unset) AND explicit v4 both hide the banner, because the
 *   default is now v4 and there is nothing to warn about
 *
 * Note: the `?ui=v3` cookie override cannot be read from a server
 * component at build time, so this banner only reflects the env-var
 * state. Operators using the cookie override will see the (v3) shell
 * without this banner — acceptable because the cookie override is a
 * deliberate user action, not a deployment default.
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
  if (uiVersion !== "v3") {
    return null;
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="w-full border-b border-amber-700/40 bg-amber-950/40 px-4 py-2 text-center text-xs text-amber-200"
    >
      v4 is the new default as of v0.6.2. You are viewing console v3
      because{" "}
      <code className="rounded bg-amber-900/40 px-1 py-0.5 font-mono">
        NEXT_PUBLIC_CONSOLE_UI_VERSION=v3
      </code>{" "}
      is set. v3 will be removed in v0.7.
    </div>
  );
}
