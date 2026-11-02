"use client";

// The guard around every signed-in page (src/app/(app)/layout.jsx).
//
// It runs in the browser only: the server can't know whether someone is
// signed in, because the access token lives in this tab's memory and the
// refresh cookie is only sent to /api/v1/auth (ADR 022). It decides what
// to SHOW; the API itself still refuses every request without a valid token.

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth";
import { loginPathFor } from "@/lib/safe-redirect";

export function RequireAuth({ children }) {
  const { status } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (status === "anonymous") {
      // replace, not push: "back" shouldn't return to a page that only
      // redirects again. ?next= brings the user back here after logging in.
      router.replace(loginPathFor(pathname));
    }
  }, [status, router, pathname]);

  if (status === "authenticated") {
    return children;
  }
  return (
    <p role="status" className="text-muted mx-auto my-16 text-center">
      {status === "loading" ? "Checking your session…" : "Taking you to the login page…"}
    </p>
  );
}
