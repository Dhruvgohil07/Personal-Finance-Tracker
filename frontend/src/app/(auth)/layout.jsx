// The layout of the pages for visitors who are NOT signed in: /login and
// /register. "(auth)" in the folder name is a "route group": it groups
// pages under one layout without appearing in the URL.

import { Suspense } from "react";

import { RedirectIfSignedIn } from "@/components/redirect-if-signed-in";

export default function AuthLayout({ children }) {
  return (
    <main className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-6 px-4 py-16">
      <p className="text-lg font-semibold">Kharcha</p>
      {/* RedirectIfSignedIn reads ?next= from the URL, which only exists in
          the browser; Next.js requires that part to sit inside <Suspense>
          so the rest of the page can still be prerendered. */}
      <Suspense fallback={null}>
        <RedirectIfSignedIn />
      </Suspense>
      {children}
    </main>
  );
}
