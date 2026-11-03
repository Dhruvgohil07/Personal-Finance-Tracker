"use client";

// The uploads page's content: the form to upload a statement, then every
// upload so far, newest first.

import Link from "next/link";

import { FormError } from "@/components/form-error";
import { UploadForm } from "@/components/uploads/upload-form";
import { UploadList } from "@/components/uploads/upload-list";
import { useAccounts } from "@/lib/accounts";
import { formErrorsFrom } from "@/lib/form-errors";
import { useUploads } from "@/lib/uploads";

export function UploadsView() {
  const accounts = useAccounts();
  const uploads = useUploads();

  if (accounts.isPending) {
    return <p role="status">Loading…</p>;
  }
  if (accounts.error) {
    return <FormError message={formErrorsFrom(accounts.error).formError} />;
  }
  if (accounts.data.length === 0) {
    return (
      <section className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Upload a statement</h1>
        <p className="text-muted">
          A statement belongs to one of your bank accounts, so{" "}
          <Link href="/accounts" className="underline">
            add an account
          </Link>{" "}
          first.
        </p>
      </section>
    );
  }

  return (
    <div className="flex flex-col gap-8">
      <section className="flex max-w-md flex-col gap-3">
        <h1 className="text-2xl font-semibold">Upload a statement</h1>
        <UploadForm accounts={accounts.data} />
      </section>
      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">Your uploads</h2>
        {uploads.isPending ? <p role="status">Loading your uploads…</p> : null}
        {uploads.error ? <FormError message={formErrorsFrom(uploads.error).formError} /> : null}
        {uploads.data?.length === 0 ? <p className="text-muted">No uploads yet.</p> : null}
        {uploads.data?.length ? (
          <UploadList uploads={uploads.data} accounts={accounts.data} />
        ) : null}
      </section>
    </div>
  );
}
