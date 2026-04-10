"use client";

import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { TimeAgo } from "@/components/TimeAgo";
import { TableSkeleton } from "@/components/Skeleton";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

interface ConsoleUser {
  user_id: string;
  username: string;
  role: string;
  disabled: boolean;
  created_at: string | null;
  updated_at: string | null;
}

async function fetchUsers(): Promise<ConsoleUser[]> {
  const res = await fetch(`${BASE}/api/auth/users`, { credentials: "include" });
  if (!res.ok) throw new Error(`Failed to fetch users: ${res.status}`);
  return res.json();
}

async function createUser(
  username: string,
  password: string,
  role: string
): Promise<void> {
  const res = await fetch(`${BASE}/api/auth/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ username, password, role }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: "Failed" }));
    throw new Error(body.detail || `Error ${res.status}`);
  }
}

async function updateUser(
  userId: string,
  updates: { role?: string; disabled?: boolean }
): Promise<void> {
  const res = await fetch(`${BASE}/api/auth/users/${userId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(updates),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: "Failed" }));
    throw new Error(body.detail || `Error ${res.status}`);
  }
}

function CreateUserForm({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("viewer");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await createUser(username, password, role);
      setUsername("");
      setPassword("");
      setRole("viewer");
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create user");
    } finally {
      setLoading(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-wrap items-end gap-3">
      <div>
        <label className="block text-xs font-medium mb-1" style={{ color: "var(--text-secondary)" }}>
          Username
        </label>
        <input
          type="text"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
          className="px-3 py-2 text-sm border outline-none w-40"
          style={{
            background: "var(--card)",
            borderColor: "var(--border)",
            borderRadius: "var(--radius-sm)",
            color: "var(--fg)",
          }}
          placeholder="newuser"
        />
      </div>
      <div>
        <label className="block text-xs font-medium mb-1" style={{ color: "var(--text-secondary)" }}>
          Password
        </label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          minLength={8}
          className="px-3 py-2 text-sm border outline-none w-40"
          style={{
            background: "var(--card)",
            borderColor: "var(--border)",
            borderRadius: "var(--radius-sm)",
            color: "var(--fg)",
          }}
          placeholder="min 8 chars"
        />
      </div>
      <div>
        <label className="block text-xs font-medium mb-1" style={{ color: "var(--text-secondary)" }}>
          Role
        </label>
        <select
          value={role}
          onChange={(e) => setRole(e.target.value)}
          className="px-3 py-2 text-sm border outline-none"
          style={{
            background: "var(--card)",
            borderColor: "var(--border)",
            borderRadius: "var(--radius-sm)",
            color: "var(--fg)",
          }}
        >
          <option value="viewer">viewer</option>
          <option value="admin">admin</option>
        </select>
      </div>
      <button
        type="submit"
        disabled={loading}
        className="px-4 py-2 text-sm font-medium text-white disabled:opacity-50 transition-colors"
        style={{ background: "var(--accent)", borderRadius: "var(--radius-sm)" }}
      >
        {loading ? "Creating..." : "Create user"}
      </button>
      {error && (
        <span className="text-sm" style={{ color: "var(--danger)" }}>
          {error}
        </span>
      )}
    </form>
  );
}

function UserRow({
  user,
  currentUserId,
  onUpdate,
}: {
  user: ConsoleUser;
  currentUserId: string;
  onUpdate: () => void;
}) {
  const [updating, setUpdating] = useState(false);
  const isSelf = user.user_id === currentUserId;

  async function toggleRole() {
    setUpdating(true);
    try {
      await updateUser(user.user_id, {
        role: user.role === "admin" ? "viewer" : "admin",
      });
      onUpdate();
    } finally {
      setUpdating(false);
    }
  }

  async function toggleDisabled() {
    setUpdating(true);
    try {
      await updateUser(user.user_id, { disabled: !user.disabled });
      onUpdate();
    } finally {
      setUpdating(false);
    }
  }

  return (
    <tr className="border-b hover:bg-white/[0.02] transition-colors" style={{ borderColor: "var(--border)" }}>
      <td className="py-3 pr-3">
        <div className="flex items-center gap-2">
          <span
            className="w-6 h-6 rounded-full flex items-center justify-center text-xs font-medium text-white"
            style={{ background: user.disabled ? "var(--text-tertiary)" : "var(--accent)" }}
          >
            {user.username.charAt(0).toUpperCase()}
          </span>
          <span className="text-sm font-medium">{user.username}</span>
        </div>
      </td>
      <td className="py-3 pr-3">
        <span
          className="inline-flex items-center px-2 py-0.5 text-xs font-medium border"
          style={{
            borderRadius: "var(--radius-sm)",
            background:
              user.role === "admin"
                ? "rgba(130, 40, 245, 0.1)"
                : "rgba(255, 255, 255, 0.04)",
            borderColor:
              user.role === "admin"
                ? "rgba(130, 40, 245, 0.3)"
                : "var(--border)",
            color:
              user.role === "admin" ? "var(--accent-light)" : "var(--text-secondary)",
          }}
        >
          {user.role}
        </span>
      </td>
      <td className="py-3 pr-3">
        {user.disabled ? (
          <span className="text-xs" style={{ color: "var(--danger)" }}>Disabled</span>
        ) : (
          <span className="text-xs" style={{ color: "var(--success)" }}>Active</span>
        )}
      </td>
      <td className="py-3 pr-3 text-xs" style={{ color: "var(--text-tertiary)" }}>
        <TimeAgo iso={user.created_at} />
      </td>
      <td className="py-3 text-right">
        {!isSelf && (
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={toggleRole}
              disabled={updating}
              className="text-xs px-2 py-1 border transition-colors hover:border-[var(--accent)] disabled:opacity-50"
              style={{
                borderColor: "var(--border)",
                borderRadius: "var(--radius-sm)",
                color: "var(--text-secondary)",
              }}
            >
              {user.role === "admin" ? "Demote" : "Promote"}
            </button>
            <button
              onClick={toggleDisabled}
              disabled={updating}
              className="text-xs px-2 py-1 border transition-colors disabled:opacity-50"
              style={{
                borderColor: user.disabled ? "rgba(34, 197, 94, 0.3)" : "rgba(239, 68, 68, 0.3)",
                borderRadius: "var(--radius-sm)",
                color: user.disabled ? "var(--success)" : "var(--danger)",
              }}
            >
              {user.disabled ? "Enable" : "Disable"}
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}

export default function UsersPage() {
  const { user: currentUser } = useAuth();
  const queryClient = useQueryClient();
  const { data: users, isLoading, error } = useQuery({
    queryKey: ["admin-users"],
    queryFn: fetchUsers,
  });

  function refresh() {
    queryClient.invalidateQueries({ queryKey: ["admin-users"] });
  }

  if (currentUser?.role !== "admin") {
    return (
      <div className="text-center py-20">
        <h2 className="text-xl font-semibold mb-2" style={{ color: "var(--danger)" }}>
          Access denied
        </h2>
        <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
          User management requires admin privileges.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight mb-1">User Management</h1>
        <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
          Create and manage console users. All users authenticate via session cookies with {8}-hour TTL.
        </p>
      </div>

      <section
        className="p-4 border"
        style={{
          background: "var(--card)",
          borderColor: "var(--border)",
          borderRadius: "var(--radius-md)",
        }}
      >
        <h2 className="text-sm font-semibold mb-3">Create New User</h2>
        <CreateUserForm onSuccess={refresh} />
      </section>

      <section>
        <h2 className="text-sm font-semibold mb-3">
          All Users{" "}
          {users && (
            <span className="font-normal" style={{ color: "var(--text-tertiary)" }}>
              ({users.length})
            </span>
          )}
        </h2>
        {isLoading ? (
          <TableSkeleton rows={4} />
        ) : error ? (
          <div className="text-center py-8">
            <p style={{ color: "var(--danger)" }}>Failed to load users</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left border-b" style={{ borderColor: "var(--border)", color: "var(--text-tertiary)" }}>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">User</th>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Role</th>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Status</th>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Created</th>
                  <th className="py-2 font-medium text-xs uppercase tracking-wider text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {users?.map((u) => (
                  <UserRow
                    key={u.user_id}
                    user={u}
                    currentUserId={currentUser?.user_id ?? ""}
                    onUpdate={refresh}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
