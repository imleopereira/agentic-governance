"use client";
import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { api } from "@/lib/api";
import {
  LayoutGrid, Activity, CheckSquare, ScrollText,
  DollarSign, Users, ChevronLeft, ChevronRight, LogOut, Shield, FileCheck,
} from "lucide-react";

interface NavItem {
  label: string; href: string; icon: React.ElementType;
  exact?: boolean; adminOnly?: boolean; showBadge?: boolean;
}

// FIX 5: when `NEXT_PUBLIC_CONSOLE_UI_VERSION === "v4"`, the middleware
// rewrites `/` to `/agents`, so a Topology link at `href: "/"` never
// highlights. Replace it with a proper "Agents" entry in v4; leave the
// v3 nav untouched.
//
// v0.6.2 (Agent E): add `Compliance` entry to the v4 nav so
// the Article 12 report is reachable from the primary IA, not just the
// header pill. The route `/compliance` is the only v4-only page beyond
// `/agents`; the rest of the v4 nav items (`/stream`, `/gates`,
// `/events`, `/cost`, `/admin/users`) do NOT have `(v4)/*` counterparts
// yet and transparently hand off to the v3 pages at `app/<route>/`
// because route-group parentheses don't change URL paths. The v4 shell
// (fixed header pill, ErrorBoundary, DisconnectBanner) does NOT wrap
// those handoff pages — this is acceptable for Wave 1.5; Wave 2+ will
// re-parent routes under `(v4)/` one by one.
//
// v3 nav deliberately does NOT include Compliance: `(v4)/compliance`'s
// layout calls `notFound()` when `NEXT_PUBLIC_CONSOLE_UI_VERSION !== "v4"`,
// so a Compliance entry in v3 would route v3 users straight to a 404.
const IS_V4 = process.env.NEXT_PUBLIC_CONSOLE_UI_VERSION === "v4";

const NAV_ITEMS: NavItem[] = IS_V4
  ? [
      { label: "Agents", href: "/agents", icon: LayoutGrid },
      { label: "Compliance", href: "/compliance", icon: FileCheck },
      { label: "Event Stream", href: "/stream", icon: Activity },
      { label: "Approvals", href: "/gates", icon: CheckSquare, showBadge: true },
      { label: "Audit Log", href: "/events", icon: ScrollText },
      { label: "Cost", href: "/cost", icon: DollarSign },
      { label: "Users", href: "/admin/users", icon: Users, adminOnly: true },
    ]
  : [
      { label: "Topology", href: "/", icon: LayoutGrid, exact: true },
      { label: "Event Stream", href: "/stream", icon: Activity },
      { label: "Approvals", href: "/gates", icon: CheckSquare, showBadge: true },
      { label: "Audit Log", href: "/events", icon: ScrollText },
      { label: "Cost", href: "/cost", icon: DollarSign },
      { label: "Users", href: "/admin/users", icon: Users, adminOnly: true },
    ];

function isActive(href: string, pathname: string, exact?: boolean): boolean {
  if (exact) return pathname === href;
  return pathname.startsWith(href);
}

export function Sidebar() {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  // v0.6.2: badge must be visible on ALL routes, not just when `/gates` is
  // active. The Approvals page also owns `["gates-pending"]` and refetches
  // every 5s, so when the operator is on that page the shared query cache
  // stays fresh; from any other page the Sidebar's own 30s refetch keeps
  // the count reasonably current without hammering the API. If the fetch
  // fails (`data` undefined), we render no badge rather than `?`.
  const { data: pendingGates } = useQuery({
    queryKey: ["gates-pending"],
    queryFn: api.gatesPending,
    refetchInterval: 30_000,
    enabled: !!user,
  });
  const pendingCount = pendingGates?.length ?? 0;
  const [collapsed, setCollapsed] = useState(false);
  const w = collapsed ? 48 : 260;

  return (
    <aside
      aria-label="Primary navigation"
      style={{
        width: w, minWidth: w,
        background: "var(--sidebar-bg)",
        borderRight: "1px solid var(--border)",
        display: "flex", flexDirection: "column",
        transition: "width var(--transition-slow), min-width var(--transition-slow)",
        overflow: "hidden", position: "relative", zIndex: 10,
      }}
    >
      {/* Wordmark */}
      <div style={{
        height: 56, display: "flex", alignItems: "center",
        padding: collapsed ? "0 0 0 14px" : "0 16px",
        borderBottom: "1px solid var(--border)",
        flexShrink: 0, overflow: "hidden", whiteSpace: "nowrap",
      }}>
        <Shield size={18} style={{ color: "var(--accent)", flexShrink: 0 }} aria-hidden="true" />
        {!collapsed && (
          <span style={{
            marginLeft: 8, fontFamily: "var(--font-mono)",
            fontSize: "0.8125rem", color: "var(--accent)", fontWeight: 500,
          }}>
            governance
          </span>
        )}
      </div>

      {/* Nav items */}
      <nav style={{ flex: 1, padding: "8px 0", overflowY: "auto" }} aria-label="Main navigation">
        {NAV_ITEMS.filter((item) => !item.adminOnly || user?.role === "admin").map((item) => {
          const active = isActive(item.href, pathname, item.exact);
          const Icon = item.icon;
          const badge = item.showBadge && pendingCount > 0 ? pendingCount : 0;
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              title={collapsed ? item.label : undefined}
              style={{
                display: "flex", alignItems: "center", gap: 10,
                padding: collapsed ? "10px 0 10px 15px" : "9px 14px",
                margin: "1px 6px", borderRadius: "var(--radius-sm)",
                fontSize: "0.875rem", fontWeight: active ? 500 : 400,
                color: active ? "var(--fg)" : "var(--text-secondary)",
                background: active ? "rgba(130, 40, 245, 0.1)" : "transparent",
                textDecoration: "none",
                transition: "background var(--transition-fast), color var(--transition-fast)",
                position: "relative", overflow: "hidden", whiteSpace: "nowrap",
              }}
              onMouseEnter={(e) => { if (!active) e.currentTarget.style.background = "rgba(255,255,255,0.04)"; }}
              onMouseLeave={(e) => { if (!active) e.currentTarget.style.background = "transparent"; }}
            >
              {active && (
                <span aria-hidden="true" style={{
                  position: "absolute", left: 0, top: "20%", height: "60%",
                  width: 2, background: "var(--accent)", borderRadius: "0 2px 2px 0",
                }} />
              )}
              <span style={{ position: "relative", flexShrink: 0 }}>
                <Icon size={16} aria-hidden="true"
                  style={{ color: active ? "var(--accent-light)" : "inherit" }} />
                {badge > 0 && collapsed && (
                  <span aria-label={`${badge} pending`} style={{
                    position: "absolute", top: -4, right: -4,
                    background: "var(--warn)", color: "#1a1a1a",
                    fontFamily: "var(--font-mono)",
                    fontSize: "0.625rem", fontWeight: 700, minWidth: 14, height: 14,
                    borderRadius: 7, display: "flex", alignItems: "center",
                    justifyContent: "center", padding: "0 2px",
                  }}>
                    {badge > 99 ? "99+" : badge}
                  </span>
                )}
              </span>
              {!collapsed && (
                <>
                  <span style={{ flex: 1 }}>{item.label}</span>
                  {badge > 0 && (
                    <span aria-label={`${badge} pending`} style={{
                      background: "var(--warn)", color: "#1a1a1a",
                      fontFamily: "var(--font-mono)",
                      fontSize: "0.6875rem", fontWeight: 600, minWidth: 18, height: 18,
                      borderRadius: 9, display: "inline-flex", alignItems: "center",
                      justifyContent: "center", padding: "0 6px", flexShrink: 0,
                    }}>
                      {badge > 99 ? "99+" : badge}
                    </span>
                  )}
                </>
              )}
            </Link>
          );
        })}
      </nav>

      {/* Bottom: user info + collapse toggle */}
      <div style={{ borderTop: "1px solid var(--border)", padding: "8px 6px", flexShrink: 0 }}>
        {!collapsed && user && (
          <div style={{
            display: "flex", alignItems: "center", gap: 8,
            padding: "8px 10px", marginBottom: 2,
          }}>
            <span aria-hidden="true" style={{
              width: 24, height: 24, borderRadius: "50%",
              background: "var(--accent)", color: "#fff",
              fontSize: "0.6875rem", fontWeight: 600,
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              flexShrink: 0,
            }}>
              {user.username.charAt(0).toUpperCase()}
            </span>
            <div style={{ flex: 1, overflow: "hidden" }}>
              <p style={{
                fontSize: "0.8125rem", color: "var(--fg)", fontWeight: 500,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}>{user.username}</p>
              <p style={{ fontSize: "0.6875rem", color: "var(--text-tertiary)" }}>{user.role}</p>
            </div>
            <button onClick={logout} title="Sign out" aria-label="Sign out"
              style={{
                background: "none", border: "none", cursor: "pointer",
                color: "var(--text-tertiary)", padding: 4,
                borderRadius: "var(--radius-sm)", display: "flex", alignItems: "center",
              }}>
              <LogOut size={14} aria-hidden="true" />
            </button>
          </div>
        )}
        <button
          onClick={() => setCollapsed((v) => !v)}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          style={{
            width: "100%", display: "flex", alignItems: "center",
            justifyContent: collapsed ? "center" : "flex-start",
            gap: 8, padding: collapsed ? "8px 0" : "7px 10px",
            background: "none", border: "none", cursor: "pointer",
            color: "var(--text-tertiary)", borderRadius: "var(--radius-sm)",
            fontSize: "0.75rem", transition: "background var(--transition-fast)",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.04)")}
          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
        >
          {collapsed ? <ChevronRight size={14} aria-hidden="true" /> : (
            <><ChevronLeft size={14} aria-hidden="true" /><span>Collapse</span></>
          )}
        </button>
      </div>
    </aside>
  );
}
