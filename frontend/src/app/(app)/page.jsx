"use client";

// The home page for signed-in users. A placeholder until the dashboard
// (Phase 3 Step 9).

import { useMe } from "@/lib/me";

export default function HomePage() {
  const me = useMe();

  return (
    <section className="flex flex-col gap-2">
      <h1 className="text-2xl font-semibold">
        {me.data ? `Welcome, ${me.data.name}` : "Welcome"}
      </h1>
      <p className="text-muted">
        Your accounts, statements and spending will appear here as Kharcha grows.
      </p>
    </section>
  );
}
