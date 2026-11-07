"use client";

// "Your profile": the name and email from GET /me. Read-only, because the
// API has no way to change them yet. useMe() shares its answer with the
// header, so opening this page usually asks the API nothing new.

import { FormError } from "@/components/form-error";
import { formErrorsFrom } from "@/lib/form-errors";
import { useMe } from "@/lib/me";

export function ProfileSection() {
  const me = useMe();

  return (
    <section aria-labelledby="profile-heading" className="flex flex-col gap-3">
      <h2 id="profile-heading" className="text-lg font-semibold">
        Your profile
      </h2>
      {me.error ? <FormError message={formErrorsFrom(me.error).formError} /> : null}
      {me.isPending ? <p role="status">Loading your profile…</p> : null}
      {me.data ? (
        // A <dl> pairs each label with its value for screen readers.
        <dl className="grid grid-cols-[6rem_1fr] gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted">Name</dt>
          <dd>{me.data.name}</dd>
          <dt className="text-muted">Email</dt>
          <dd className="break-all">{me.data.email}</dd>
        </dl>
      ) : null}
    </section>
  );
}
