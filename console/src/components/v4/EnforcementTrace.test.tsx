/**
 * F1 EnforcementTrace — runtime render + assert tests.
 *
 * v0.6.1 polish sprint: replaces the v0.6 source-grep test that
 * read EnforcementTrace.tsx off disk and regex-matched the
 * `EnforcementTraceProps` interface body for "only has `event`".
 * The runtime test below asserts the observable consequence: the
 * component renders from a single `event` prop, with no extra prop
 * required to produce the 5-gate vector.
 *
 * The branch-by-branch gate inference is still covered by
 * `EnforcementTrace.gate-inference.test.tsx` — that file already uses
 * real imports (no source grep) and was NOT part of the 8 LLM-theater
 * tripwire.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { EnforcementTrace } from "./EnforcementTrace";
import type { AuditEvent } from "@/lib/api";
import { SDK_KINDS } from "@/lib/sdkKinds";

function makeEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    event_id: "evt-abc",
    session_id: "sess-12345678",
    agent_id: "agent-a",
    parent_event_id: null,
    kind: SDK_KINDS.TOOL_CALL,
    model: null,
    input_hash: null,
    output_hash: null,
    metadata: {},
    prev_hash: null,
    hmac_value: "deadbeef",
    chain_seq: 42,
    created_at: "2026-04-15T12:00:00.000Z",
    ...overrides,
  };
}

describe("EnforcementTrace — prop contract (runtime)", () => {
  it("renders from a single `event` prop (no sessionEvents needed)", () => {
    render(<EnforcementTrace event={makeEvent()} />);
    // Section landmark is present and keyed on the event id.
    expect(
      screen.getByRole("region", { name: /Enforcement trace for event evt-abc/ }),
    ).toBeInTheDocument();
  });

  it("renders the 5 canonical gates in order", () => {
    render(<EnforcementTrace event={makeEvent()} />);
    const list = screen.getByRole("list", { name: /Enforcement gate chain/i });
    const items = list.querySelectorAll("li");
    expect(items).toHaveLength(5);
    // Each <li> contains the gate label as a plain text node. Grab the
    // ordered list of labels via the aria-labelled status spans, which
    // carry the gate name as the subject of their `aria-label`.
    const statusLabels = Array.from(
      list.querySelectorAll<HTMLElement>("span[aria-label]"),
    ).map((el) => (el.getAttribute("aria-label") ?? "").split(" gate:")[0]);
    expect(statusLabels).toEqual([
      "Scope",
      "Budget",
      "HITL",
      "Contract",
      "Audit",
    ]);
  });

  it("surfaces the BLOCKED pill on a scope violation", () => {
    render(
      <EnforcementTrace
        event={makeEvent({
          kind: SDK_KINDS.SCOPE_VIOLATION,
          metadata: { tool: "rm_rf" },
        })}
      />,
    );
    expect(screen.getByText("BLOCKED")).toBeInTheDocument();
    // Scope gate must announce FAIL in its aria-label.
    const scopeStatus = screen.getByLabelText(/Scope gate: fail/i);
    expect(scopeStatus).toBeInTheDocument();
    // And the detail text names the tool.
    expect(
      screen.getByText(/tool "rm_rf" NOT in allowlist/i),
    ).toBeInTheDocument();
  });

  it("does NOT show BLOCKED on a clean tool call", () => {
    render(<EnforcementTrace event={makeEvent()} />);
    expect(screen.queryByText("BLOCKED")).toBeNull();
    // All five gates announce PASS / SKIP — never FAIL.
    expect(screen.queryAllByLabelText(/gate: fail/i)).toHaveLength(0);
  });

  it("renders manual integration mode with the limited-coverage caveat", () => {
    render(<EnforcementTrace event={makeEvent()} />);
    expect(
      screen.getByText(/manual \(limited coverage\)/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Only gates explicitly called in application code/i),
    ).toBeInTheDocument();
  });

  it("renders wrap_openai integration (full coverage, no caveat)", () => {
    render(
      <EnforcementTrace
        event={makeEvent({ kind: SDK_KINDS.LLM_CALL, model: "gpt-4o" })}
      />,
    );
    expect(
      screen.getByText(/wrap_openai \/ wrap_anthropic \(full coverage\)/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Only gates explicitly called/i),
    ).toBeNull();
  });
});
