"use client";

// The home page for signed-in users.
//
// First run, one step at a time, since each needs the one before (SPEC §12:
// value in under 2 minutes):
//   1. no accounts yet   → "Add your first account"
//   2. no uploads yet    → "Upload your first statement"
//   3. otherwise         → the dashboard (Phase 3 Step 9). Above it, the
//                          latest upload while it still needs something:
//                          it is importing, needs its password or a check,
//                          or failed. A plain "Imported" is left out,
//                          unless the user watched it import on this page:
//                          then what it added stays in view.
//
// Suspense: the dashboard reads the URL's ?month= with useSearchParams,
// which Next.js only knows in the browser; `next build` requires a
// boundary around it (see the transactions page).

import { Suspense, useState } from "react";

import { AccountForm } from "@/components/accounts/account-form";
import { Dashboard } from "@/components/dashboard/dashboard";
import { FormError } from "@/components/form-error";
import { UploadForm } from "@/components/uploads/upload-form";
import { UploadList } from "@/components/uploads/upload-list";
import { useAccounts } from "@/lib/accounts";
import { formErrorsFrom } from "@/lib/form-errors";
import { useMe } from "@/lib/me";
import { isUnfinished, useUploads } from "@/lib/uploads";

function FirstRunStep({ greeting, title, text, children }) {
  return (
    <section className="flex max-w-md flex-col gap-4">
      <h1 className="text-2xl font-semibold">{greeting}</h1>
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold">{title}</h2>
        <p className="text-muted text-sm">{text}</p>
      </div>
      {children}
    </section>
  );
}

export default function HomePage() {
  const me = useMe();
  const accounts = useAccounts();
  const uploads = useUploads();
  const greeting = me.data ? `Welcome, ${me.data.name}` : "Welcome";

  // Uploads come newest first, so [0] is the latest.
  const latest = uploads.data?.[0];
  // The upload this page saw importing, so its result stays shown once it
  // is "Imported". Remembered with React's "adjust state while rendering"
  // pattern: a state update during render, guarded so it happens once per
  // upload, makes React render again at once with the new value. An effect
  // would do the same a frame later, after the card had already vanished.
  const [watchedId, setWatchedId] = useState(null);
  if (latest && isUnfinished(latest.status) && watchedId !== latest.id) {
    setWatchedId(latest.id);
  }

  // Each step waits only for what it needs: the first step needs just the
  // accounts, so a failing GET /uploads must not hide it (review of PR 33).
  if (accounts.error) {
    return <FormError message={formErrorsFrom(accounts.error).formError} />;
  }
  if (accounts.isPending) {
    return <p role="status">Loading…</p>;
  }

  if (accounts.data.length === 0) {
    return (
      <FirstRunStep
        greeting={greeting}
        title="Add your first account"
        text="Tell Kharcha which bank account your statements come from. Then you can upload your first statement and see where your money goes."
      >
        <AccountForm submitLabel="Add my first account" />
      </FirstRunStep>
    );
  }

  if (uploads.error) {
    return <FormError message={formErrorsFrom(uploads.error).formError} />;
  }
  if (uploads.isPending) {
    return <p role="status">Loading…</p>;
  }

  if (uploads.data.length === 0) {
    return (
      <FirstRunStep
        greeting={greeting}
        title="Upload your first statement"
        text="Download a statement from your bank's netbanking and upload it here. Kharcha reads every transaction and sorts it into categories."
      >
        <UploadForm accounts={accounts.data} submitLabel="Upload my first statement" />
      </FirstRunStep>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">{greeting}</h1>
      {latest.status !== "completed" || latest.id === watchedId ? (
        <section className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">Your latest statement</h2>
          <UploadList uploads={[latest]} accounts={accounts.data} />
        </section>
      ) : null}
      <Suspense fallback={<p role="status">Loading your month…</p>}>
        <Dashboard />
      </Suspense>
    </div>
  );
}
