"use client";

// The home page for signed-in users.
//
// First run, one step at a time, since each needs the one before (SPEC §12:
// value in under 2 minutes):
//   1. no accounts yet   → "Add your first account"
//   2. no uploads yet    → "Upload your first statement"
//   3. otherwise         → the latest upload: while it imports, its status;
//                          then what it added (or its password prompt).
// The dashboard (Phase 3 Step 9) will take the place of the placeholder text.

import Link from "next/link";

import { AccountForm } from "@/components/accounts/account-form";
import { FormError } from "@/components/form-error";
import { UploadForm } from "@/components/uploads/upload-form";
import { UploadList } from "@/components/uploads/upload-list";
import { useAccounts } from "@/lib/accounts";
import { formErrorsFrom } from "@/lib/form-errors";
import { useMe } from "@/lib/me";
import { useUploads } from "@/lib/uploads";

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

  const error = accounts.error ?? uploads.error;
  if (error) {
    return <FormError message={formErrorsFrom(error).formError} />;
  }
  if (accounts.isPending || uploads.isPending) {
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
      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">Your latest statement</h2>
        {/* Uploads come newest first, so [0] is the latest. */}
        <UploadList uploads={uploads.data.slice(0, 1)} accounts={accounts.data} />
      </section>
      <p className="text-muted">
        <Link href="/uploads" className="underline">
          Upload another statement
        </Link>{" "}
        or{" "}
        <Link href="/accounts" className="underline">
          manage your accounts
        </Link>
        . Your spending will appear here as Kharcha grows.
      </p>
    </div>
  );
}
