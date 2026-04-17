"use client";

import { useEffect, useState, useRef, useCallback, type ReactNode } from "react";

interface Step {
  target: string;
  title: string;
  body: string;
}

// v0.6.2: tour rewritten for v4 IA + compliance-officer voice.
//   - Default route is /agents (v4 flip). Sidebar has Compliance.
//   - Terminology: halt (not kill). Article 12 evidence export is the marquee.
//   - Anchors use stable sidebar hrefs + existing data-testids so the tour
//     survives visual redesigns. Steps that would need a drawer-open state
//     (e.g. the Trail tab at `#drill-tab-trail`) instead anchor on the
//     always-visible compliance header pill and explain the drill-in path
//     in copy — Walkthrough.tsx does NOT navigate between routes, so every
//     anchor must be reachable from wherever the operator starts the tour.
const STEPS: Step[] = [
  {
    target: "a[href='/agents']",
    title: "Your agent fleet",
    body: "Every agent wrapped with the SDK appears in this list. The status dot shows live, idle, or halted at a glance. Click any row to drill into its audit trail, scope policy, budget, and recent sessions.",
  },
  {
    target: "[data-testid='compliance-header-pill']",
    title: "Tamper-evident audit",
    body: "Every event is HMAC-chained and Ed25519-signed per row. This pill reports the live verification result for the whole chain -- one click re-verifies and tells you exactly which row was tampered with, if any.",
  },
  {
    target: "[data-testid='compliance-header-pill']",
    title: "Halt means halt, everywhere",
    body: "Halting an agent fails every enforcement path closed within five seconds -- scope checks, cost gates, HITL approvals, and outbound LLM wrappers all reject. This pill flips red the moment the chain reports halted, so you never miss it.",
  },
  {
    target: "a[href='/gates']",
    title: "Pending human reviews",
    body: "HITL gates that need a reviewer claim live under Approvals. The badge on this nav item shows how many signed, single-use tokens are waiting for a decision.",
  },
  {
    target: "a[href='/compliance']",
    title: "Article 12 evidence",
    body: "One-click export of the signed evidence bundle for EU AI Act Article 12. Open Compliance and hit Export to hand your auditor a timestamped, chain-verified record -- no spreadsheet reconciliation needed.",
  },
];

const STORAGE_KEY = "governance_walkthrough_completed";

function ProgressDots({ current, total }: { current: number; total: number }) {
  return (
    <div className="flex items-center gap-1.5">
      {Array.from({ length: total }, (_, i) => (
        <button
          key={i}
          className="transition-all"
          style={{
            width: i === current ? "16px" : "6px",
            height: "6px",
            borderRadius: "3px",
            background: i === current ? "var(--accent)" : i < current ? "var(--accent-light)" : "var(--border)",
            opacity: i === current ? 1 : i < current ? 0.6 : 0.4,
          }}
          aria-label={`Step ${i + 1}`}
          tabIndex={-1}
        />
      ))}
    </div>
  );
}

function TargetHighlight({ target }: { target: string }) {
  const [rect, setRect] = useState<DOMRect | null>(null);

  const measure = useCallback(() => {
    const el = document.querySelector(target);
    if (el) {
      setRect(el.getBoundingClientRect());
    } else {
      setRect(null);
    }
  }, [target]);

  useEffect(() => {
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [measure]);

  if (!rect) return null;

  const padding = 6;
  return (
    <div
      className="fixed pointer-events-none transition-all"
      style={{
        top: rect.top - padding,
        left: rect.left - padding,
        width: rect.width + padding * 2,
        height: rect.height + padding * 2,
        borderRadius: "var(--radius-md)",
        border: "2px solid var(--accent)",
        boxShadow: "0 0 0 4px rgba(130, 40, 245, 0.15), 0 0 20px rgba(130, 40, 245, 0.1)",
        zIndex: 51,
      }}
    />
  );
}

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
        <div className="fixed bottom-6 right-6 max-w-sm z-50 animate-fade-in-up">
          <div
            className="border p-5"
            style={{
              background: "var(--card)",
              borderColor: "rgba(130, 40, 245, 0.2)",
              borderRadius: "var(--radius-lg)",
              boxShadow: "var(--shadow-accent)",
            }}
          >
            <h3 className="font-bold text-base mb-1">
              Welcome to the Governance Console
            </h3>
            <p className="text-sm mb-4" style={{ color: "var(--text-tertiary)" }}>
              Tamper-evident audit, halt-across-every-path enforcement, and
              one-click Article 12 evidence export for your AI agents.
              Take the five-step tour?
            </p>
            <div className="flex gap-2">
              <button
                onClick={startTour}
                className="px-4 py-2 text-sm font-medium text-white transition-colors hover:brightness-110"
                style={{ background: "var(--accent)", borderRadius: "var(--radius-sm)" }}
              >
                Start tour
              </button>
              <button
                onClick={() => {
                  setDismissed(true);
                  localStorage.setItem(STORAGE_KEY, "true");
                }}
                className="px-4 py-2 text-sm transition-colors hover:brightness-125"
                style={{ color: "var(--text-tertiary)" }}
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
          {/* Blurred backdrop */}
          <div
            className="absolute inset-0"
            style={{
              background: "rgba(0, 0, 0, 0.65)",
              backdropFilter: "blur(6px)",
              WebkitBackdropFilter: "blur(6px)",
            }}
            onClick={finish}
          />

          {/* Target highlight */}
          <TargetHighlight target={current.target} />

          {/* Step card */}
          <div className="absolute bottom-8 left-1/2 -translate-x-1/2 max-w-lg w-full mx-4 z-50">
            <div
              className="border p-6 animate-fade-in-up"
              style={{
                background: "var(--card)",
                borderColor: "rgba(130, 40, 245, 0.15)",
                borderRadius: "var(--radius-lg)",
                boxShadow: "0 4px 32px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(130, 40, 245, 0.1)",
              }}
            >
              {/* Segmented progress bar */}
              <div className="flex gap-1 mb-4">
                {STEPS.map((_, i) => (
                  <div
                    key={i}
                    className="h-1 flex-1 transition-all"
                    style={{
                      background: i <= step ? "var(--accent)" : "var(--border)",
                      borderRadius: "2px",
                      opacity: i <= step ? 1 : 0.5,
                    }}
                  />
                ))}
              </div>

              <p
                className="text-xs font-medium uppercase tracking-wider mb-1"
                style={{ color: "var(--accent)", letterSpacing: "0.15em" }}
              >
                Step {step + 1} of {STEPS.length}
              </p>
              <h3 className="font-bold text-lg mb-2">{current.title}</h3>
              <p className="text-sm leading-relaxed mb-5" style={{ color: "var(--text-secondary)" }}>
                {current.body}
              </p>

              {/* Progress dots */}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-4">
                  <button
                    onClick={finish}
                    className="text-sm transition-colors hover:brightness-125"
                    style={{ color: "var(--text-tertiary)" }}
                  >
                    Skip tour
                  </button>
                  <ProgressDots current={step} total={STEPS.length} />
                </div>
                <div className="flex gap-2">
                  {step > 0 && (
                    <button
                      onClick={prev}
                      className="px-4 py-2 border text-sm font-medium hover:bg-white/5 transition-colors flex items-center gap-1.5"
                      style={{
                        borderColor: "var(--border)",
                        borderRadius: "var(--radius-sm)",
                        color: "var(--text-secondary)",
                      }}
                    >
                      <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="10 13 5 8 10 3" />
                      </svg>
                      Back
                    </button>
                  )}
                  <button
                    onClick={next}
                    className="px-5 py-2 text-sm font-medium text-white transition-colors hover:brightness-110 flex items-center gap-1.5"
                    style={{
                      background: "var(--accent)",
                      borderRadius: "var(--radius-sm)",
                      boxShadow: "0 2px 8px rgba(130, 40, 245, 0.3)",
                    }}
                  >
                    {step === STEPS.length - 1 ? "Finish" : "Next"}
                    {step < STEPS.length - 1 && (
                      <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="6 3 11 8 6 13" />
                      </svg>
                    )}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Restart tour button */}
      {dismissed && (
        <button
          onClick={resetTour}
          data-tour="restart"
          className="fixed bottom-4 right-4 z-40 w-8 h-8 flex items-center justify-center border text-xs transition-all hover:border-[var(--accent)] hover:text-[var(--accent)]"
          style={{
            borderColor: "var(--border)",
            background: "var(--card)",
            color: "var(--text-tertiary)",
            borderRadius: "var(--radius-sm)",
          }}
          title="Restart the walkthrough tour"
        >
          ?
        </button>
      )}
    </>
  );
}
