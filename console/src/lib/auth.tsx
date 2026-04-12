"use client";

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";

import { queryClient } from "@/lib/queryClient";

export interface AuthUser {
  user_id: string;
  username: string;
  role: string;
}

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  loading: true,
  login: async () => {},
  logout: async () => {},
  refresh: async () => {},
});

export function useAuth() {
  return useContext(AuthContext);
}

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${BASE}/api/auth/me`, {
        credentials: "include",
      });
      if (res.ok) {
        const data = await res.json();
        setUser(data);
      } else {
        setUser(null);
      }
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Also check for legacy token in localStorage
    const token = localStorage.getItem("governance_token");
    if (token) {
      // Legacy token mode: fetch /api/auth/me with bearer
      fetch(`${BASE}/api/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      })
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (data) setUser(data);
          setLoading(false);
        })
        .catch(() => {
          setLoading(false);
        });
    } else {
      refresh();
    }
  }, [refresh]);

  const login = useCallback(
    async (username: string, password: string) => {
      const res = await fetch(`${BASE}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: "Login failed" }));
        throw new Error(body.detail || `Login failed (${res.status})`);
      }
      await refresh();
    },
    [refresh]
  );

  const logout = useCallback(async () => {
    // SECURITY (H1): clear all cached query data FIRST so data fetched
    // while user A was logged in cannot leak into user B's session if
    // they log in on the same browser tab.
    queryClient.clear();
    localStorage.removeItem("governance_token");
    try {
      await fetch(`${BASE}/api/auth/logout`, {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // Ignore network errors on logout
    }
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}
