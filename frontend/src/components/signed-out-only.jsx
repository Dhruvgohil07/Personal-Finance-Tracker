"use client";

// Around the forms of /login and /register: shows them only while nobody
// is signed in. Once a session starts, the form is UNMOUNTED, so its state
// (the email, the password, "Logging in…") is gone.
//
// Why that matters: with `cacheComponents` (next.config.mjs), Next.js keeps
// a page you navigated away from mounted but hidden, React state included,
// so the Back button can show it instantly. The login form, left behind
// after a successful login, kept the typed password; after a logout, or
// after deleting the account, /login showed the previous user's email and
// password again, with the button stuck on "Logging in…" (found in the
// Phase 3 Step 10 live check). Unmounting on sign-in means a session that
// ends always comes back to an empty form.
//
// RedirectIfSignedIn (in the same layout) does the navigating; this only
// decides what is on screen meanwhile.

import { useAuth } from "@/lib/auth";

export function SignedOutOnly({ children }) {
  const { status } = useAuth();
  if (status === "authenticated") {
    return <p role="status">Signing you in…</p>;
  }
  return children;
}
