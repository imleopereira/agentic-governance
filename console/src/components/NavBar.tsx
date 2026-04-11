"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import Link from "next/link";

const NAV_LINKS = [
  { label: "Posture", href: "/", tourKey: "posture" },
  { label: "Audit Log", href: "/events", tourKey: "nav-events" },
  { label: "Cost", href: "/cost", tourKey: "nav-cost" },
  { label: "Gates", href: "/gates", tourKey: "nav-gates" },
];

const PIP_SNIPPET = "pip install code-atelier-governance";

export function NavBar() {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const handleCopyPip = useCallback(() => {
    navigator.clipboard.writeText(PIP_SNIPPET).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }, []);

  return (
    <nav className="border-b px-6 py-3 flex items-center justify-between" style={{ borderColor: "var(--border)" }}>
      <div className="flex items-center gap-6">
        {/* Wordmark */}
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm" style={{ color: "var(--text-tertiary)" }}>{"{ "}</span>
          <span className="font-mono text-sm font-medium" style={{ color: "var(--accent)" }}>governance</span>
          <span className="font-mono text-sm" style={{ color: "var(--text-tertiary)" }}>{" }"}</span>
          <span className="text-xs ml-1" style={{ color: "var(--text-tertiary)" }}>v0.2.2</span>
        </div>

        {/* Separator */}
        <div className="hidden md:block w-px h-5" style={{ background: "var(--border)" }} />

        {/* Nav links */}
        <div className="hidden md:flex gap-1">
          {NAV_LINKS.map((link) => {
            const isActive =
              link.href === "/"
                ? pathname === "/"
                : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                data-tour={link.tourKey}
                className={`px-3 py-1.5 text-sm transition-colors ${
                  isActive
                    ? "text-[var(--fg)] font-medium"
                    : "hover:text-[var(--fg)]"
                }`}
                style={{
                  color: isActive ? "var(--fg)" : "var(--text-tertiary)",
                  background: isActive ? "rgba(130, 40, 245, 0.08)" : "transparent",
                  borderBottom: isActive ? "2px solid var(--accent)" : "2px solid transparent",
                  borderRadius: isActive ? "var(--radius-sm) var(--radius-sm) 0 0" : undefined,
                  marginBottom: "-13px",
                  paddingBottom: "11px",
                }}
              >
                {link.label}
              </Link>
            );
          })}
          {user?.role === "admin" && (
            <Link
              href="/admin/users"
              className={`px-3 py-1.5 text-sm transition-colors ${
                pathname.startsWith("/admin")
                  ? "text-[var(--fg)] font-medium"
                  : "hover:text-[var(--fg)]"
              }`}
              style={{
                color: pathname.startsWith("/admin") ? "var(--fg)" : "var(--text-tertiary)",
                background: pathname.startsWith("/admin") ? "rgba(130, 40, 245, 0.08)" : "transparent",
                borderBottom: pathname.startsWith("/admin") ? "2px solid var(--accent)" : "2px solid transparent",
                borderRadius: pathname.startsWith("/admin") ? "var(--radius-sm) var(--radius-sm) 0 0" : undefined,
                marginBottom: "-13px",
                paddingBottom: "11px",
              }}
            >
              Users
            </Link>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3">
        {/* Pip install snippet */}
        <div className="relative hidden lg:block">
          <button
            onClick={handleCopyPip}
            className="flex items-center gap-1.5 px-2 py-1 text-xs font-mono transition-colors hover:border-[var(--accent)]"
            style={{
              color: "var(--text-tertiary)",
              background: "rgba(130, 40, 245, 0.06)",
              border: "1px solid rgba(130, 40, 245, 0.15)",
              borderRadius: "var(--radius-sm)",
              cursor: "pointer",
            }}
            title="Click to copy"
          >
            <span style={{ color: "var(--text-tertiary)" }}>$</span>
            <span>{PIP_SNIPPET}</span>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="ml-1 opacity-50">
              <rect x="5" y="5" width="9" height="9" rx="1.5" />
              <path d="M5 11H3.5A1.5 1.5 0 0 1 2 9.5V3.5A1.5 1.5 0 0 1 3.5 2h6A1.5 1.5 0 0 1 11 3.5V5" />
            </svg>
          </button>
          {copied && (
            <span
              className="absolute -bottom-7 left-1/2 -translate-x-1/2 px-2 py-0.5 text-xs font-medium whitespace-nowrap animate-fade-in-up"
              style={{
                background: "var(--accent)",
                color: "white",
                borderRadius: "var(--radius-sm)",
              }}
            >
              Copied!
            </span>
          )}
        </div>

        {/* Separator */}
        <div className="hidden lg:block w-px h-5" style={{ background: "var(--border)" }} />

        {/* User menu */}
        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className="flex items-center gap-2 text-sm px-3 py-1.5 border transition-colors hover:border-[var(--accent)]"
            style={{
              borderColor: menuOpen ? "var(--accent)" : "var(--border)",
              borderRadius: "var(--radius-sm)",
              color: "var(--text-secondary)",
            }}
          >
            <span className="w-5 h-5 rounded-full flex items-center justify-center text-xs font-medium" style={{ background: "var(--accent)", color: "white" }}>
              {user?.username?.charAt(0).toUpperCase() ?? "?"}
            </span>
            <span className="hidden sm:inline">{user?.username ?? "unknown"}</span>
            <svg
              width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
              style={{
                color: "var(--text-tertiary)",
                transform: menuOpen ? "rotate(180deg)" : "rotate(0deg)",
                transition: "transform 150ms ease",
              }}
            >
              <path d="M3 4.5L6 7.5L9 4.5" />
            </svg>
          </button>

          {menuOpen && (
            <div
              className="absolute right-0 top-full mt-1.5 w-52 py-1 border shadow-lg z-50 animate-fade-in-up"
              style={{
                background: "var(--elevated)",
                borderColor: "var(--border)",
                borderRadius: "var(--radius-md)",
              }}
            >
              <div className="px-3 py-2.5 border-b" style={{ borderColor: "var(--border)" }}>
                <div className="flex items-center gap-2 mb-1">
                  <p className="text-sm font-medium">{user?.username}</p>
                  <span
                    className="px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider"
                    style={{
                      background: user?.role === "admin" ? "rgba(130, 40, 245, 0.15)" : "rgba(255, 255, 255, 0.06)",
                      color: user?.role === "admin" ? "var(--accent-light)" : "var(--text-tertiary)",
                      borderRadius: "3px",
                      border: user?.role === "admin" ? "1px solid rgba(130, 40, 245, 0.25)" : "1px solid var(--border)",
                    }}
                  >
                    {user?.role}
                  </span>
                </div>
                <p className="text-xs font-mono" style={{ color: "var(--text-tertiary)" }}>
                  {user?.user_id?.slice(0, 8)}...
                </p>
              </div>
              {user?.role === "admin" && (
                <Link
                  href="/admin/users"
                  onClick={() => setMenuOpen(false)}
                  className="flex items-center gap-2 px-3 py-2 text-sm transition-colors hover:bg-white/5"
                  style={{ color: "var(--text-secondary)" }}
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M11 14v-1a3 3 0 0 0-3-3H5a3 3 0 0 0-3 3v1" />
                    <circle cx="6.5" cy="5" r="2.5" />
                    <path d="M13 6v4M11 8h4" />
                  </svg>
                  Manage users
                </Link>
              )}
              <div className="my-1 mx-2" style={{ height: "1px", background: "var(--border)" }} />
              <button
                onClick={() => {
                  setMenuOpen(false);
                  logout();
                }}
                className="w-full text-left flex items-center gap-2 px-3 py-2 text-sm transition-colors hover:bg-white/5"
                style={{ color: "var(--danger)" }}
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M6 14H3.5A1.5 1.5 0 0 1 2 12.5v-9A1.5 1.5 0 0 1 3.5 2H6" />
                  <path d="M10.5 11.5L14 8l-3.5-3.5" />
                  <path d="M14 8H6" />
                </svg>
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </nav>
  );
}
