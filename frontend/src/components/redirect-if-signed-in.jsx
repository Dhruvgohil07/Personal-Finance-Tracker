"use client";

// On /login and /register: once there is a session (already signed in, or
// the form just logged in), go to the page in ?next=, or home.
//
// This is the ONE place that navigates after a login, so the login and
// register forms only have to start the session.

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth";
import { safeNextPath } from "@/lib/safe-redirect";

export function RedirectIfSignedIn() {
  const { status } = useAuth();
  const router = useRouter();
  // Reading the URL's query makes this part of the page render in the
  // browser only; the layout wraps it in <Suspense> for that reason.
  const next = useSearchParams().get("next");

  useEffect(() => {
    if (status === "authenticated") {
      router.replace(safeNextPath(next));
    }
  }, [status, router, next]);

  return null;
}
