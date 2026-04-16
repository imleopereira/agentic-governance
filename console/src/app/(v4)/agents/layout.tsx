"use client";

/**
 * v4 Agents IA — parallel-routes layout.
 *
 * The list is rendered by this layout directly (not by `page.tsx`) so it
 * persists across /agents and /agents/[id] navigations. The `@drill`
 * parallel slot renders DrillPanel into the right aside, but ONLY when
 * the URL matches /agents/[id]. On /agents, the aside is fully collapsed
 * so the list gets the full viewport width.
 *
 * Why a pathname regex instead of trusting `default.tsx` returning null:
 * Next 15 parallel routes have a known quirk where the `@drill` slot
 * state can lag by one navigation when routing from /agents/[id] back to
 * /agents — the slot may still render the previous DrillPanel mid-flight.
 * Gating the aside on the pathname guarantees the panel goes away the
 * instant the URL changes, regardless of slot cache timing.
 */

import type { ReactNode, MouseEvent } from "react";
import { usePathname, useRouter } from "next/navigation";
import AgentsList from "@/components/v4/AgentsList";

export default function AgentsLayout({
  children,
  drill,
}: {
  children: ReactNode;
  drill: ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  // Match /agents/[anything], but NOT bare /agents or /agents/ trailing slash.
  const hasDrill = /^\/agents\/[^/]+/.test(pathname ?? "");

  // Click-outside handler: when a drill is open and the operator clicks
  // anywhere in the list column that is NOT a row (rows are <Link>s and
  // their clicks navigate to the new agent directly), collapse the drill
  // by navigating back to /agents.  Using onMouseDown with a target check
  // so row clicks still reach the Link first — this only fires when the
  // click started on the outer container itself.
  const onListMouseDown = (e: MouseEvent<HTMLDivElement>) => {
    if (!hasDrill) return;
    if (e.target !== e.currentTarget) return;
    router.replace("/agents");
  };

  return (
    <div className="flex h-full min-h-screen">
      <div
        className="flex-1 min-w-0 overflow-auto"
        onMouseDown={onListMouseDown}
      >
        <AgentsList />
        {/* `page.tsx` and `[id]/page.tsx` render null — kept for routing. */}
        {children}
      </div>
      {hasDrill && (
        <aside
          className="w-[440px] shrink-0 overflow-hidden relative z-40"
          style={{ borderLeft: "1px solid var(--border)", background: "var(--bg)" }}
          aria-label="Agent detail"
        >
          {drill}
        </aside>
      )}
    </div>
  );
}
