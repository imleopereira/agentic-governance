"use client";

import { useState, type FormEvent } from "react";
import { useAuth } from "@/lib/auth";

export function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(username, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center px-4"
      style={{
        background: `
          radial-gradient(ellipse 80% 60% at 50% 0%, rgba(130, 40, 245, 0.08) 0%, transparent 60%),
          radial-gradient(ellipse 50% 40% at 50% 100%, rgba(130, 40, 245, 0.04) 0%, transparent 50%),
          var(--bg)
        `,
      }}
    >
      <div className="w-full max-w-sm animate-fade-in-up">
        {/* Branding */}
        <div className="text-center mb-8">
          {/* Shield icon */}
          <div className="flex items-center justify-center mb-4">
            <div
              className="w-12 h-12 flex items-center justify-center"
              style={{
                background: "rgba(130, 40, 245, 0.12)",
                border: "1px solid rgba(130, 40, 245, 0.2)",
                borderRadius: "var(--radius-md)",
              }}
            >
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                <path d="M9 12l2 2 4-4" />
              </svg>
            </div>
          </div>

          <div className="flex items-center justify-center gap-2 mb-2">
            <span className="font-mono text-sm" style={{ color: "var(--text-tertiary)" }}>{"{ "}</span>
            <span className="font-mono text-lg font-medium" style={{ color: "var(--accent)" }}>governance</span>
            <span className="font-mono text-sm" style={{ color: "var(--text-tertiary)" }}>{" }"}</span>
          </div>
          <p className="text-xs font-medium tracking-widest uppercase mb-4" style={{ color: "var(--text-tertiary)" }}>
            Governance Console
          </p>
          <h1 className="text-xl font-semibold tracking-tight mb-1">Sign in to Console</h1>
          <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            Enforcement gates dashboard for AI agent governance
          </p>
        </div>

        {/* Login form */}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="username" className="block text-xs font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Username
            </label>
            <input
              id="username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              required
              className="w-full px-3 py-2.5 text-sm border outline-none transition-all"
              style={{
                background: "var(--card)",
                borderColor: "var(--border)",
                borderRadius: "var(--radius-sm)",
                color: "var(--fg)",
              }}
              onFocus={(e) => {
                e.currentTarget.style.borderColor = "var(--accent)";
                e.currentTarget.style.boxShadow = "0 0 0 3px var(--accent-glow), 0 0 12px rgba(130, 40, 245, 0.1)";
              }}
              onBlur={(e) => {
                e.currentTarget.style.borderColor = "var(--border)";
                e.currentTarget.style.boxShadow = "none";
              }}
              placeholder="admin"
            />
          </div>
          <div>
            <label htmlFor="password" className="block text-xs font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Password
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
              className="w-full px-3 py-2.5 text-sm border outline-none transition-all"
              style={{
                background: "var(--card)",
                borderColor: "var(--border)",
                borderRadius: "var(--radius-sm)",
                color: "var(--fg)",
              }}
              onFocus={(e) => {
                e.currentTarget.style.borderColor = "var(--accent)";
                e.currentTarget.style.boxShadow = "0 0 0 3px var(--accent-glow), 0 0 12px rgba(130, 40, 245, 0.1)";
              }}
              onBlur={(e) => {
                e.currentTarget.style.borderColor = "var(--border)";
                e.currentTarget.style.boxShadow = "none";
              }}
            />
          </div>

          {error && (
            <div
              className="flex items-center gap-2 text-sm px-3 py-2.5 border"
              style={{
                color: "var(--danger)",
                background: "rgba(239, 68, 68, 0.08)",
                borderColor: "rgba(239, 68, 68, 0.2)",
                borderRadius: "var(--radius-sm)",
              }}
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="shrink-0">
                <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.5" />
                <path d="M8 5v3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                <circle cx="8" cy="11" r="0.75" fill="currentColor" />
              </svg>
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading || !username || !password}
            className="w-full py-3 text-sm font-semibold text-white transition-all disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
            style={{
              background: "linear-gradient(135deg, var(--accent) 0%, var(--accent-light) 100%)",
              borderRadius: "var(--radius-sm)",
              boxShadow: loading || !username || !password ? "none" : "0 0 20px rgba(130, 40, 245, 0.25), 0 2px 8px rgba(0, 0, 0, 0.3)",
            }}
          >
            {loading ? (
              <span className="flex items-center justify-center gap-2">
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                Signing in...
              </span>
            ) : (
              "Sign in"
            )}
          </button>
        </form>

        {/* Footer hint */}
        <p className="text-center text-xs mt-6" style={{ color: "var(--text-tertiary)" }}>
          First time? Run{" "}
          <code
            className="px-1.5 py-0.5 text-xs font-mono"
            style={{
              background: "rgba(130, 40, 245, 0.1)",
              borderRadius: "3px",
              border: "1px solid rgba(130, 40, 245, 0.2)",
            }}
          >
            governance console add-user
          </code>{" "}
          to create your admin account.
        </p>

        {/* Dev mode hint */}
        <p className="text-center text-xs mt-3" style={{ color: "var(--text-tertiary)" }}>
          Or set{" "}
          <code
            className="px-1.5 py-0.5 text-xs font-mono"
            style={{
              background: "rgba(130, 40, 245, 0.1)",
              borderRadius: "3px",
              border: "1px solid rgba(130, 40, 245, 0.2)",
            }}
          >
            GOVERNANCE_CONSOLE_DEV_MODE=true
          </code>{" "}
          to skip auth.
        </p>
      </div>
    </div>
  );
}
