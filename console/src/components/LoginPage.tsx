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
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm animate-fade-in-up">
        {/* Branding */}
        <div className="text-center mb-8">
          <div className="flex items-center justify-center gap-2 mb-3">
            <span className="font-mono text-sm" style={{ color: "var(--text-tertiary)" }}>{"{ "}</span>
            <span className="font-mono text-lg font-medium" style={{ color: "var(--accent)" }}>governance</span>
            <span className="font-mono text-sm" style={{ color: "var(--text-tertiary)" }}>{" }"}</span>
          </div>
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
              className="w-full px-3 py-2.5 text-sm border outline-none transition-colors"
              style={{
                background: "var(--card)",
                borderColor: "var(--border)",
                borderRadius: "var(--radius-sm)",
                color: "var(--fg)",
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
              className="w-full px-3 py-2.5 text-sm border outline-none transition-colors"
              style={{
                background: "var(--card)",
                borderColor: "var(--border)",
                borderRadius: "var(--radius-sm)",
                color: "var(--fg)",
              }}
            />
          </div>

          {error && (
            <div
              className="text-sm px-3 py-2 border"
              style={{
                color: "var(--danger)",
                background: "rgba(239, 68, 68, 0.08)",
                borderColor: "rgba(239, 68, 68, 0.2)",
                borderRadius: "var(--radius-sm)",
              }}
            >
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading || !username || !password}
            className="w-full py-2.5 text-sm font-medium text-white transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            style={{
              background: "var(--accent)",
              borderRadius: "var(--radius-sm)",
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
            governance create-user
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
