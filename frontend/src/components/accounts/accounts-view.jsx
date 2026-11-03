"use client";

// The accounts page's content: the list, and the form to add one.

import { AccountForm } from "@/components/accounts/account-form";
import { AccountRow } from "@/components/accounts/account-row";
import { FormError } from "@/components/form-error";
import { useAccounts } from "@/lib/accounts";
import { formErrorsFrom } from "@/lib/form-errors";

export function AccountsView() {
  const accounts = useAccounts();

  return (
    <div className="flex flex-col gap-8">
      <section className="flex flex-col gap-3">
        <h1 className="text-2xl font-semibold">Your accounts</h1>
        {accounts.isPending ? <p role="status">Loading your accounts…</p> : null}
        {accounts.error ? <FormError message={formErrorsFrom(accounts.error).formError} /> : null}
        {accounts.data?.length === 0 ? (
          <p className="text-muted">No accounts yet. Add your first one below.</p>
        ) : null}
        {accounts.data?.length ? (
          <ul className="flex flex-col gap-3">
            {accounts.data.map((account) => (
              <AccountRow key={account.id} account={account} />
            ))}
          </ul>
        ) : null}
      </section>
      <section className="flex max-w-md flex-col gap-3">
        <h2 className="text-lg font-semibold">Add an account</h2>
        <AccountForm />
      </section>
    </div>
  );
}
