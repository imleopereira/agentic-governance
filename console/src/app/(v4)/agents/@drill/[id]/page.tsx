"use client";
// The parallel-routes slot renders the drill-over whenever /agents/[id] is
// active. `open` is always true in this render — the route itself is the
// "open" state. `onClose` navigates back to /agents, which unmounts this
// segment via Next.js App Router.
import { useRouter } from "next/navigation";
import { use } from "react";
import { notFound } from "next/navigation";

import { DrillPanel } from "@/components/v4/DrillPanel";

export default function AgentDrillPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const router = useRouter();
  const { id } = use(params);
  // Security M4: guard against malformed %-sequences in route params.
  let agentId: string;
  try {
    agentId = decodeURIComponent(id);
  } catch {
    notFound();
  }
  // `router.replace` (not push) avoids stacking an extra history entry
  // every time the user closes a drill, and more importantly forces the
  // parallel `@drill` slot to re-resolve — `router.push` can leave the
  // slot content cached in Next 15.2+.  The layout-level pathname gate
  // (see `../../layout.tsx`) then hides the aside entirely.
  return (
    <DrillPanel
      agentId={agentId}
      open={true}
      onClose={() => router.replace("/agents")}
    />
  );
}
