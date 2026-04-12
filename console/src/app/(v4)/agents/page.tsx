/**
 * v4 Agents list route — intentionally empty.
 *
 * The list itself is rendered by `(v4)/agents/layout.tsx` via
 * `<AgentsList />` so it remains mounted when an operator drills into
 * `/agents/[id]`. This page exists only so Next.js has a valid leaf for
 * `/agents`.
 */
export default function AgentsPage() {
  return null;
}
