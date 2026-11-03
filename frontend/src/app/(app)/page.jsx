"use client";

// The home page for signed-in users.
//
// First run: a user with no accounts yet sees "Add your first account"
// straight away, since nothing else works without one (SPEC §12: value in
// under 2 minutes). Step 6 adds the next first-run step, the first upload.
// Otherwise a placeholder until the dashboard (Phase 3 Step 9).

import Link from "next/link";

import { AccountForm } from "@/components/accounts/account-form";
import { FormError } from "@/components/form-error";
import { useAccounts } from "@/lib/accounts";
import { formErrorsFrom } from "@/lib/form-errors";
import { useMe } from "@/lib/me";

export default function HomePage() {
  const me = useMe();
  const accounts = useAccounts();
  const greeting = me.data ? `Welcome, ${me.data.name}` : "Welcome";

  if (accounts.isPending) {
    return <p role="status">Loading…</p>;
  }
  if (accounts.error) {
    return <FormError message={formErrorsFrom(accounts.error).formError} />;
  }

  if (accounts.data.length === 0) {
    return (
      <section className="flex max-w-md flex-col gap-4">
        <h1 className="text-2xl font-semibold">{greeting}</h1>
        <div className="flex flex-col gap-1">
          <h2 className="text-lg font-semibold">Add your first account</h2>
          <p className="text-muted text-sm">
            Tell Kharcha which bank account your statements come from. Then you can upload your
            first statement and see where your money goes.
          </p>
        </div>
        <AccountForm submitLabel="Add my first account" />
      </section>
    );
  }

  return (
    <section className="flex flex-col gap-2">
      <h1 className="text-2xl font-semibold">{greeting}</h1>
      <p className="text-muted">
        You have {accounts.data.length} {accounts.data.length === 1 ? "account" : "accounts"}.{" "}
        <Link href="/accounts" className="underline">
          Manage your accounts
        </Link>
        . Your statements and spending will appear here as Kharcha grows.
      </p>
    </section>
  );
}
