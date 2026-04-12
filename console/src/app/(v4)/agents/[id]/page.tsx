/**
 * v4 Agents `[id]` main-slot route — intentionally empty.
 *
 * The list keeps rendering from the layout; the drill panel fills the
 * aside via the `@drill/[id]` parallel slot. This file exists only so
 * Next.js App Router has a valid leaf for `/agents/[id]` in the
 * `children` slot.
 */
export default function AgentDetailPage() {
  return null;
}
