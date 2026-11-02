"use client";

// The home page. A placeholder until Step 4 (auth pages) and Step 9
// (dashboard); for now it shows that the session restore works.

import { useAuth } from "@/lib/auth";

const STATUS_TEXT = {
  loading: "Checking your session…",
  authenticated: "You are signed in.",
  anonymous: "You are not signed in.",
};

export default function HomePage() {
  const { status } = useAuth();

  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center gap-4 px-4 py-16">
      <h1 className="text-3xl font-semibold">Kharcha</h1>
      <p className="text-muted">See where your money goes, from your own bank statements.</p>
      <p role="status">{STATUS_TEXT[status]}</p>
    </main>
  );
}
