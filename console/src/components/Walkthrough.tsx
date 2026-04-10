"use client";

import { useEffect, useState, type ReactNode } from "react";

interface Step {
  target: string; // CSS selector for the element to highlight
  title: string;
  body: string;
  position?: "top" | "bottom" | "left" | "right";
}

const STEPS: Step[] = [
  {
    target: "[data-tour='posture']",
    title: "Governance Posture",
    body: "This is your command center. Each card represents one AI agent in your system. The four badges — Scope, Cost, Gates, Audit — show whether each enforcement module is healthy (PASS), needs attention (WARN), or has a violation (FAIL). One glance tells you the governance state of your entire fleet.",
  },
  {
    target: "[data-tour='status-badge']",
    title: "Status Badges",
    body: "PASS means the module is healthy with no violations today. WARN means something needs attention (e.g. pending approvals or high spend). FAIL means a violation occurred — a scope breach, a budget overrun, or a tampered audit row.",
  },
  {
    target: "[data-tour='nav-events']",
    title: "Audit Log Explorer",
    body: "Every action your agents take is logged here as an immutable, HMAC-chained audit event. Filter by agent, event kind, or session. Click a session ID to see the full chain, then hit 'Verify chain' to cryptographically prove no row has been tampered with.",
  },
  {
    target: "[data-tour='nav-cost']",
    title: "Cost Dashboard",
    body: "See exactly how much each agent spent today — in dollars and tokens. Drill into individual sessions to find which calls cost the most. Budget caps are enforced in real time by the SDK; this dashboard shows you the aftermath.",
  },
  {
    target: "[data-tour='nav-gates']",
    title: "Approval Gates",
    body: "High-risk actions can require human approval before they execute. This page shows pending requests waiting for a human decision, and the history of recently granted or denied approvals. Each approval is a signed, single-use token bound to the specific action.",
  },
  {
    target: "[data-tour='verify']",
    title: "Chain Integrity Verification",
    body: "This is the compliance officer's money feature. Click 'Verify' on any session and the server re-computes the HMAC of every audit row from scratch. If any row was modified — even one byte of metadata — the verification fails and tells you exactly which event was tampered with. The HMAC secret never leaves the server.",
  },
];

const STORAGE_KEY = "governance_walkthrough_completed";

export function WalkthroughProvider({ children }: { children: ReactNode }) {
  const [active, setActive] = useState(false);
  const [step, setStep] = useState(0);
  const [dismissed, setDismissed] = useState(true);

  useEffect(() => {
    const done = localStorage.getItem(STORAGE_KEY);
    if (!done) {
      setDismissed(false);
    }
  }, []);

  function startTour() {
    setActive(true);
    setStep(0);
  }

  function next() {
    if (step < STEPS.length - 1) {
      setStep(step + 1);
    } else {
      finish();
    }
  }

  function prev() {
    if (step > 0) setStep(step - 1);
  }

  function finish() {
    setActive(false);
    setDismissed(true);
    localStorage.setItem(STORAGE_KEY, "true");
  }

  function resetTour() {
    localStorage.removeItem(STORAGE_KEY);
    setDismissed(false);
    startTour();
  }

  const current = STEPS[step];

  return (
    <>
      {children}

      {/* Welcome banner for first-time users */}
      {!dismissed && !active && (
        <div className="fixed bottom-6 right-6 max-w-sm z-50 animate-in fade-in slide-in-from-bottom-4 duration-500">
          <div className="rounded-xl border border-[var(--accent)]/30 bg-[var(--card)] shadow-2xl shadow-blue-500/10 p-5">
            <h3 className="font-bold text-base mb-1">
              Welcome to the Governance Console
            </h3>
            <p className="text-sm text-[var(--muted)] mb-4">
              This dashboard shows the enforcement state of your AI agents —
              audit trails, cost caps, scope policies, and approval gates.
              Take a quick tour?
            </p>
            <div className="flex gap-2">
              <button
                onClick={startTour}
                className="px-4 py-2 rounded-lg bg-[var(--accent)] text-white text-sm font-medium hover:bg-blue-600 transition"
              >
                Start tour
              </button>
              <button
                onClick={() => {
                  setDismissed(true);
                  localStorage.setItem(STORAGE_KEY, "true");
                }}
                className="px-4 py-2 rounded-lg text-sm text-[var(--muted)] hover:text-white transition"
              >
                Skip
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Tour overlay */}
      {active && (
        <div className="fixed inset-0 z-50">
          {/* Dimmed backdrop */}
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={finish}
          />

          {/* Tooltip */}
          <div className="absolute bottom-8 left-1/2 -translate-x-1/2 max-w-lg w-full mx-4 z-50">
            <div className="rounded-xl border border-[var(--border)] bg-[var(--card)] shadow-2xl p-6">
              {/* Progress bar */}
              <div className="flex gap-1 mb-4">
                {STEPS.map((_, i) => (
                  <div
                    key={i}
                    className={`h-1 flex-1 rounded-full transition-colors ${
                      i <= step ? "bg-[var(--accent)]" : "bg-[var(--border)]"
                    }`}
                  />
                ))}
              </div>

              <p className="text-xs text-[var(--accent)] font-medium uppercase tracking-wide mb-1">
                Step {step + 1} of {STEPS.length}
              </p>
              <h3 className="font-bold text-lg mb-2">{current.title}</h3>
              <p className="text-sm text-[var(--muted)] leading-relaxed mb-5">
                {current.body}
              </p>

              <div className="flex items-center justify-between">
                <button
                  onClick={finish}
                  className="text-sm text-[var(--muted)] hover:text-white transition"
                >
                  Skip tour
                </button>
                <div className="flex gap-2">
                  {step > 0 && (
                    <button
                      onClick={prev}
                      className="px-4 py-2 rounded-lg border border-[var(--border)] text-sm hover:bg-white/5 transition"
                    >
                      Back
                    </button>
                  )}
                  <button
                    onClick={next}
                    className="px-4 py-2 rounded-lg bg-[var(--accent)] text-white text-sm font-medium hover:bg-blue-600 transition"
                  >
                    {step === STEPS.length - 1 ? "Finish" : "Next"}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Restart tour button (always visible in nav) */}
      {dismissed && (
        <button
          onClick={resetTour}
          data-tour="restart"
          className="fixed bottom-4 right-4 z-40 px-3 py-1.5 rounded-lg border border-[var(--border)] bg-[var(--card)] text-xs text-[var(--muted)] hover:text-white hover:border-[var(--accent)] transition"
          title="Restart the walkthrough tour"
        >
          ?
        </button>
      )}
    </>
  );
}
