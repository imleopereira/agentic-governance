/**
 * F4 ComplianceHeaderPill — runtime render + assert tests.
 *
 * v0.6.1 polish sprint: replaces the v0.6 source-grep test (which
 * characterised the `if (isStale(...) && !pending) reverify()` effect
 * and the `inFlight.current` guard by reading the .tsx source). Those
 * pins passed whether the component actually worked and were flagged as
 * LLM-theater; the runtime version below mounts the component, drives
 * the `api.verifyChain` mock, and asserts what the user sees.
 *
 * Includes an axe-core pass — this is the F1/F4 a11y proof the PRD
 * wanted expressed at runtime rather than source-pinned.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import {
  STALE_THRESHOLD_MS,
  formatVerifiedAt,
  isStale,
  statusLabel,
  statusToTone,
} from "./ComplianceHeaderPill.utils";

// Mock the api module so `verifyChain` is a vitest spy we can control.
vi.mock("@/lib/api", () => ({
  api: {
    verifyChain: vi.fn(),
  },
}));

import { api } from "@/lib/api";
import { ComplianceHeaderPill } from "./ComplianceHeaderPill";

type VerifyChainResult = {
  chain_integrity_status: "verified" | "unverified" | "degraded" | "halted";
  from_seq: number | null;
  to_seq: number | null;
  verified_count: number;
  failed_count: number;
  unresolved_fingerprints: string[];
  verified_at_utc: string;
};

function verifiedResponse(
  iso: string,
  status: VerifyChainResult["chain_integrity_status"] = "verified",
): VerifyChainResult {
  return {
    chain_integrity_status: status,
    from_seq: 1,
    to_seq: 10,
    verified_count: 10,
    failed_count: 0,
    unresolved_fingerprints: [],
    verified_at_utc: iso,
  };
}

describe("ComplianceHeaderPill — pure helpers (kept from v0.6)", () => {
  it("statusToTone maps the four statuses when fresh", () => {
    expect(statusToTone("verified", false)).toBe("success");
    expect(statusToTone("unverified", false)).toBe("neutral");
    expect(statusToTone("degraded", false)).toBe("warn");
    expect(statusToTone("halted", false)).toBe("danger");
  });

  it("statusToTone forces neutral when stale (DA blocker)", () => {
    expect(statusToTone("verified", true)).toBe("neutral");
    expect(statusToTone("halted", true)).toBe("neutral");
  });

  it("isStale is true when never verified", () => {
    expect(isStale(null, Date.now())).toBe(true);
  });

  it("isStale threshold is exactly 5 minutes", () => {
    expect(STALE_THRESHOLD_MS).toBe(5 * 60 * 1000);
    const now = 1_000_000_000;
    expect(isStale(now - (STALE_THRESHOLD_MS + 1), now)).toBe(true);
    expect(isStale(now - 1000, now)).toBe(false);
  });

  it("formatVerifiedAt strips seconds/ms (Cybersec MED)", () => {
    expect(formatVerifiedAt("2026-04-15T14:22:37.501Z")).toBe("14:22 UTC");
    expect(formatVerifiedAt("2026-04-15T09:05:00.000Z")).toBe("09:05 UTC");
    expect(formatVerifiedAt(null)).toBe("never");
    expect(formatVerifiedAt("not-a-date")).toBe("never");
  });

  it("statusLabel returns 'Verifying...' when stale", () => {
    expect(statusLabel("verified", true)).toBe("Verifying...");
    expect(statusLabel("halted", true)).toBe("Verifying...");
  });

  it("statusLabel returns per-status label when fresh", () => {
    expect(statusLabel("verified", false)).toBe("Chain verified");
    expect(statusLabel("unverified", false)).toBe("Chain unverified");
    expect(statusLabel("degraded", false)).toBe("Chain degraded");
    expect(statusLabel("halted", false)).toBe("Chain halted");
  });
});

describe("ComplianceHeaderPill — runtime render", () => {
  const verifyChain = api.verifyChain as unknown as ReturnType<typeof vi.fn>;

  beforeEach(() => {
    verifyChain.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("initial mount calls verifyChain once and renders the verified label", async () => {
    verifyChain.mockResolvedValueOnce(
      verifiedResponse(new Date().toISOString(), "verified"),
    );
    render(<ComplianceHeaderPill />);
    await waitFor(() => {
      expect(screen.getByText("Chain verified")).toBeInTheDocument();
    });
    expect(verifyChain).toHaveBeenCalledTimes(1);
  });

  it("renders 'Chain degraded' when backend returns degraded", async () => {
    verifyChain.mockResolvedValueOnce(
      verifiedResponse(new Date().toISOString(), "degraded"),
    );
    render(<ComplianceHeaderPill />);
    await waitFor(() => {
      expect(screen.getByText("Chain degraded")).toBeInTheDocument();
    });
  });

  it("renders 'Chain unverified' when verifyChain rejects (DA: never show stale verified)", async () => {
    verifyChain.mockRejectedValueOnce(new Error("boom"));
    render(<ComplianceHeaderPill />);
    await waitFor(() => {
      expect(screen.getByText("Chain unverified")).toBeInTheDocument();
    });
  });

  it("click triggers a re-verify and surfaces the new status", async () => {
    const user = userEvent.setup();
    verifyChain.mockResolvedValueOnce(
      verifiedResponse(new Date().toISOString(), "verified"),
    );
    render(<ComplianceHeaderPill />);
    await waitFor(() => {
      expect(screen.getByText("Chain verified")).toBeInTheDocument();
    });
    verifyChain.mockResolvedValueOnce(
      verifiedResponse(new Date().toISOString(), "halted"),
    );
    const pill = screen.getByTestId("compliance-header-pill");
    await user.click(pill);
    await waitFor(() => {
      expect(screen.getByText("Chain halted")).toBeInTheDocument();
    });
    expect(verifyChain).toHaveBeenCalledTimes(2);
  });

  it("in-flight guard collapses a burst of clicks onto a single verify", async () => {
    const user = userEvent.setup();
    // Keep the first verify pending for the duration of the click burst.
    let resolveFirst: (value: VerifyChainResult) => void = () => {};
    const firstInFlight = new Promise<VerifyChainResult>((resolve) => {
      resolveFirst = resolve;
    });
    verifyChain.mockReturnValueOnce(firstInFlight);
    render(<ComplianceHeaderPill />);
    // Initial mount kicks off the first verify; click three more times
    // while it's in-flight. The guard should drop all three.
    const pill = await screen.findByTestId("compliance-header-pill");
    await user.click(pill);
    await user.click(pill);
    await user.click(pill);
    expect(verifyChain).toHaveBeenCalledTimes(1);
    // Resolve the in-flight verify and let the component settle.
    await act(async () => {
      resolveFirst(
        verifiedResponse(new Date().toISOString(), "verified"),
      );
      await firstInFlight;
    });
    await waitFor(() => {
      expect(screen.getByText("Chain verified")).toBeInTheDocument();
    });
  });

  it("renders aria-live=polite on the pill (a11y)", async () => {
    verifyChain.mockResolvedValueOnce(
      verifiedResponse(new Date().toISOString(), "verified"),
    );
    render(<ComplianceHeaderPill />);
    const pill = await screen.findByTestId("compliance-header-pill");
    expect(pill).toHaveAttribute("aria-live", "polite");
  });

  it("passes axe-core accessibility smoke", async () => {
    verifyChain.mockResolvedValueOnce(
      verifiedResponse(new Date().toISOString(), "verified"),
    );
    const { container } = render(<ComplianceHeaderPill />);
    await waitFor(() => {
      expect(screen.getByText("Chain verified")).toBeInTheDocument();
    });
    const results = await axe.run(container, {
      // Color-contrast needs a painting browser; jsdom returns empty
      // computed styles so the rule would flag every element with a
      // CSS var. Disable it — the F1 pass already covered contrast in
      // the live-browser visual review.
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });
});
