"use client";

// The guard around every signed-in page (src/app/(app)/layout.jsx).
//
// It runs in the browser only: the server can't know whether someone is
// signed in, because the access token lives in this tab's memory and the
// refresh cookie is only sent to /api/v1/auth (ADR 022). It decides what
// to SHOW; the API itself still refuses every request without a valid token.

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth";
import { loginPathFor } from "@/lib/safe-redirect";

export function RequireAuth({ children }) {
  const { status, loggedOut } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status !== "anonymous") {
      return;
    }
    // replace, not push: "back" shouldn't return to a page that only
    // redirects again.
    if (loggedOut) {
      // The user chose to leave: plain /login, nothing about their page.
      router.replace("/login");
      return;
    }
    // No session (it expired, or this is a new visit): ?next= brings the
    // user back to this exact page after logging in, query string included
    // (e.g. /transactions?month=2026-10). Read from window.location because
    // an effect only runs in the browser; the useSearchParams() hook would
    // need a <Suspense> boundary around every signed-in page.
    const here = `${window.location.pathname}${window.location.search}`;
    router.replace(loginPathFor(here));
  }, [status, loggedOut, router]);

  if (status === "authenticated") {
    return children;
  }
  return (
    <p role="status" className="text-muted mx-auto my-16 text-center">
      {status === "loading" ? "Checking your session…" : "Taking you to the login page…"}
    </p>
  );
}
