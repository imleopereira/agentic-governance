"use client";

import { useState, useRef, useEffect } from "react";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import Link from "next/link";

const NAV_LINKS = [
  { label: "Posture", href: "/", tourKey: "posture" },
  { label: "Audit Log", href: "/events", tourKey: "nav-events" },
  { label: "Cost", href: "/cost", tourKey: "nav-cost" },
  { label: "Gates", href: "/gates", tourKey: "nav-gates" },
];

export function NavBar() {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
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
                  borderBottom: isActive ? "2px solid var(--accent)" : "2px solid transparent",
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
                borderBottom: pathname.startsWith("/admin") ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: "-13px",
                paddingBottom: "11px",
              }}
            >
              Users
            </Link>
          )}
        </div>
      </div>

      {/* User menu */}
      <div className="relative" ref={menuRef}>
        <button
          onClick={() => setMenuOpen(!menuOpen)}
          className="flex items-center gap-2 text-sm px-3 py-1.5 border transition-colors hover:border-[var(--accent)]"
          style={{
            borderColor: "var(--border)",
            borderRadius: "var(--radius-sm)",
            color: "var(--text-secondary)",
          }}
        >
          <span className="w-5 h-5 rounded-full flex items-center justify-center text-xs font-medium" style={{ background: "var(--accent)", color: "white" }}>
            {user?.username?.charAt(0).toUpperCase() ?? "?"}
          </span>
          <span className="hidden sm:inline">{user?.username ?? "unknown"}</span>
          <span className="text-xs" style={{ color: "var(--text-tertiary)" }}>
            {user?.role === "admin" ? "admin" : "viewer"}
          </span>
        </button>

        {menuOpen && (
          <div
            className="absolute right-0 top-full mt-1 w-48 py-1 border shadow-lg z-50 animate-fade-in-up"
            style={{
              background: "var(--elevated)",
              borderColor: "var(--border)",
              borderRadius: "var(--radius-md)",
            }}
          >
            <div className="px-3 py-2 border-b" style={{ borderColor: "var(--border)" }}>
              <p className="text-sm font-medium">{user?.username}</p>
              <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>
                {user?.role} / {user?.user_id?.slice(0, 8)}...
              </p>
            </div>
            {user?.role === "admin" && (
              <Link
                href="/admin/users"
                onClick={() => setMenuOpen(false)}
                className="block px-3 py-2 text-sm transition-colors hover:bg-white/5"
                style={{ color: "var(--text-secondary)" }}
              >
                Manage users
              </Link>
            )}
            <button
              onClick={() => {
                setMenuOpen(false);
                logout();
              }}
              className="w-full text-left px-3 py-2 text-sm transition-colors hover:bg-white/5"
              style={{ color: "var(--danger)" }}
            >
              Sign out
            </button>
          </div>
        )}
      </div>
    </nav>
  );
}
