"use client";

// The bar at the top of every signed-in page: the app's name, the user's
// name (GET /me) and "Log out".

import Link from "next/link";
import { useState } from "react";

import { useAuth } from "@/lib/auth";
import { useMe } from "@/lib/me";

export function AppHeader() {
  const { logout } = useAuth();
  const me = useMe();
  const [loggingOut, setLoggingOut] = useState(false);

  async function handleLogout() {
    setLoggingOut(true);
    try {
      await logout();
    } catch {
      // logout() already forgot the session in this browser even if the
      // request failed; the guard now sends the user to /login.
    }
    // No navigation here: the guard (RequireAuth) sees the session end and
    // redirects. Two places navigating at once would race each other.
  }

  return (
    <header className="border-b border-neutral-200 dark:border-neutral-800">
      <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-4 py-3">
        <Link href="/" className="text-lg font-semibold">
          Kharcha
        </Link>
        <div className="flex items-center gap-4">
          {/* The name is the user's own; React escapes it, so it can't inject HTML. */}
          <span className="text-muted text-sm">{me.data?.name ?? ""}</span>
          <button
            type="button"
            onClick={handleLogout}
            disabled={loggingOut}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-60 dark:border-neutral-700 dark:hover:bg-neutral-900"
          >
            {loggingOut ? "Logging out…" : "Log out"}
          </button>
        </div>
      </div>
    </header>
  );
}
