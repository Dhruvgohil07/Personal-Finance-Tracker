"use client";

// The bar at the top of every signed-in page: the app's name, the pages,
// the user's name (GET /me) and "Log out".

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/button";
import { useAuth } from "@/lib/auth";
import { useMe } from "@/lib/me";

const NAV_LINKS = [
  { href: "/", label: "Home" },
  { href: "/transactions", label: "Transactions" },
  { href: "/uploads", label: "Uploads" },
  { href: "/accounts", label: "Accounts" },
  { href: "/settings", label: "Settings" },
];

export function AppHeader() {
  const { logout } = useAuth();
  const me = useMe();
  const pathname = usePathname();
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
      <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-4 px-4 py-3">
        <div className="flex items-center gap-6">
          <Link href="/" className="text-lg font-semibold">
            Kharcha
          </Link>
          <nav aria-label="Main">
            {/* flex-wrap: five links don't fit beside the name on a phone. */}
            <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
              {NAV_LINKS.map((link) => (
                <li key={link.href}>
                  {/* aria-current tells screen readers which page this is;
                      the same attribute styles the link (aria-[current]:). */}
                  <Link
                    href={link.href}
                    aria-current={pathname === link.href ? "page" : undefined}
                    className="text-muted hover:text-foreground aria-[current]:text-foreground aria-[current]:font-medium"
                  >
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
        <div className="flex items-center gap-4">
          {/* The name is the user's own; React escapes it, so it can't inject HTML. */}
          <span className="text-muted text-sm">{me.data?.name ?? ""}</span>
          <Button variant="secondary" onClick={handleLogout} disabled={loggingOut}>
            {loggingOut ? "Logging out…" : "Log out"}
          </Button>
        </div>
      </div>
    </header>
  );
}
