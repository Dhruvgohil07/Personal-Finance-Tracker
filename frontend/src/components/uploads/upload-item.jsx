"use client";

// One upload in a list: its file, account and status, what the import did,
// and the actions that fit its status. Used by the uploads page (every
// upload) and the home page (the latest one).
//
// What it shows, by status:
//   pending / processing  "Importing…" (UploadsWatcher polls it meanwhile)
//   completed             how many transactions were added / already there
//   needs_review          the same, plus the rows whose balances don't add up
//   failed                the backend's error_message, written for the user
//   needs_password        a password prompt (UnlockForm)

import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/button";
import { FormError } from "@/components/form-error";
import { FormField } from "@/components/form-field";
import { formatDateTime, formatPeriod } from "@/lib/dates";
import { formErrorsFrom } from "@/lib/form-errors";
import {
  STATEMENT_PASSWORD_AUTOCOMPLETE,
  STATUS_LABELS,
  isUnfinished,
  reviewSummary,
  useDeleteUpload,
  useUnlockUpload,
} from "@/lib/uploads";

const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

// Each status's badge colours, light and dark.
const BADGE_CLASSES = {
  pending: "bg-neutral-100 text-neutral-800 dark:bg-neutral-800 dark:text-neutral-200",
  processing: "bg-neutral-100 text-neutral-800 dark:bg-neutral-800 dark:text-neutral-200",
  needs_password: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  needs_review: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  completed: "bg-green-100 text-green-900 dark:bg-green-950 dark:text-green-200",
  failed: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200",
};

export function UploadItem({ upload, accountName }) {
  // No polling here: UploadsWatcher (in the signed-in layout) polls every
  // unfinished upload and writes the answers into the cached list, which
  // re-renders this item with the newer `upload`.
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  return (
    <li className="flex flex-col gap-3 rounded-md border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          {/* break-all: a long file name without spaces would otherwise
              push the card wider than a phone screen. */}
          <p className="font-medium break-all">{upload.original_filename}</p>
          <p className="text-muted text-sm">
            {accountName} · uploaded {formatDateTime(upload.created_at)}
          </p>
        </div>
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${BADGE_CLASSES[upload.status] ?? ""}`}>
          {STATUS_LABELS[upload.status] ?? upload.status}
        </span>
      </div>

      <UploadResult upload={upload} />

      {confirmingDelete ? (
        <ConfirmDeleteUpload upload={upload} onCancel={() => setConfirmingDelete(false)} />
      ) : (
        <div>
          {/* Named after the file, like the accounts page's buttons. */}
          <Button
            variant="secondary"
            aria-label={`Delete ${upload.original_filename}`}
            onClick={() => setConfirmingDelete(true)}
          >
            Delete
          </Button>
        </div>
      )}
    </li>
  );
}

function UploadResult({ upload }) {
  if (isUnfinished(upload.status)) {
    return (
      <p role="status" className="text-sm">
        Importing… this usually takes a few seconds.
      </p>
    );
  }
  if (upload.status === "failed") {
    return (
      <p className="text-sm text-red-700 dark:text-red-400">
        {upload.error_message ?? "This statement could not be imported."}
      </p>
    );
  }
  if (upload.status === "needs_password") {
    return <UnlockForm upload={upload} />;
  }
  // completed or needs_review: the import ran.
  return (
    <div className="flex flex-col gap-2 text-sm">
      <ImportCounts upload={upload} />
      {upload.status === "needs_review" ? <ReviewNotes upload={upload} /> : null}
    </div>
  );
}

function ImportCounts({ upload }) {
  const period = formatPeriod(upload.period_start, upload.period_end);
  if (upload.rows_parsed === 0) {
    return <p>No transactions were found in this statement.</p>;
  }
  return (
    <p>
      {plural(upload.rows_inserted, "new transaction")} added
      {/* Re-uploading an overlapping statement is safe (SPEC §6.3): the
          rows already imported are recognised and skipped. */}
      {upload.rows_duplicate > 0
        ? `, ${plural(upload.rows_duplicate, "transaction")} already imported before`
        : ""}
      .{period ? ` Statement period: ${period}.` : ""}
    </p>
  );
}

function ReviewNotes({ upload }) {
  const { rows, statementIssues, more } = reviewSummary(upload);
  return (
    <div className="flex flex-col gap-1 rounded-md bg-amber-50 p-3 dark:bg-amber-950">
      <p>
        The transactions were imported, but the running balance doesn&apos;t add up. A row that
        was misread or is missing from the file is the usual cause, so compare these with your
        statement:
      </p>
      <ul className="list-disc pl-5">
        {rows.length > 0 ? (
          <li>
            {rows.length === 1 ? "Row" : "Rows"} {rows.join(", ")} of the file
          </li>
        ) : null}
        {statementIssues.map((issue) => (
          <li key={issue}>{issue}</li>
        ))}
        {more > 0 ? <li>…and {plural(more, "more problem")}</li> : null}
      </ul>
    </div>
  );
}

/**
 * The password prompt of a locked PDF (POST /uploads/{id}/password).
 *
 * A wrong password is a 400 INCORRECT_PASSWORD, and the upload keeps
 * waiting: the message goes under the field and the user types again. The
 * field is emptied after EVERY attempt, right or wrong, so the password is
 * held in the page no longer than one request. On success the upload is
 * `pending` again and this form disappears, replaced by "Importing…".
 */
function UnlockForm({ upload }) {
  const unlockUpload = useUnlockUpload();
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState({ fieldErrors: {}, formError: null });
  const inFlight = useRef(false);

  async function handleSubmit(event) {
    event.preventDefault();
    if (inFlight.current || password === "") {
      return;
    }
    inFlight.current = true;
    setErrors({ fieldErrors: {}, formError: null });
    try {
      await unlockUpload.mutateAsync({ id: upload.id, password });
    } catch (error) {
      setErrors(
        error?.code === "INCORRECT_PASSWORD"
          ? { fieldErrors: { password: error.message }, formError: null }
          : formErrorsFrom(error, ["password"]),
      );
    } finally {
      setPassword("");
      inFlight.current = false;
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex max-w-sm flex-col gap-3">
      <FormError message={errors.formError} />
      <FormField
        label={`Password for ${upload.original_filename}`}
        name="password"
        type="password"
        required
        autoComplete={STATEMENT_PASSWORD_AUTOCOMPLETE}
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        hint="This PDF is locked. The email your bank sent it with usually explains its password. It is used once to open the file and never stored."
        error={errors.fieldErrors.password}
      />
      <div>
        <Button type="submit" disabled={unlockUpload.isPending}>
          {unlockUpload.isPending ? "Unlocking…" : "Unlock and import"}
        </Button>
      </div>
    </form>
  );
}

/**
 * "Delete this upload?", inline like the accounts page's confirmation.
 *
 * Deleting undoes the import: the transactions it ADDED go with it (rows
 * it skipped as duplicates belong to an earlier upload, and stay). The
 * backend refuses with 409 in two cases, and its message says which and
 * what to do, so it is shown as it is:
 *   - the upload is being imported right now: try again once it's done;
 *   - a later statement of the same account overlaps it and relies on its
 *     rows: delete that later upload first.
 */
function ConfirmDeleteUpload({ upload, onCancel }) {
  const deleteUpload = useDeleteUpload();
  const [formError, setFormError] = useState(null);
  const cancelRef = useRef(null);
  const inFlight = useRef(false);
  const warningId = useId();

  // Focus "Cancel", not "Delete": a stray Enter must not delete anything.
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  async function handleDelete() {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setFormError(null);
    try {
      await deleteUpload.mutateAsync(upload.id);
      // Nothing else to do: the list is fetched again and this item is gone.
    } catch (error) {
      setFormError(formErrorsFrom(error).formError);
    } finally {
      inFlight.current = false;
    }
  }

  const consequence =
    upload.rows_inserted > 0
      ? `This also deletes the ${plural(upload.rows_inserted, "transaction")} it added. It can't be undone.`
      : "Nothing from it has been imported, so no transactions are deleted.";

  return (
    <div
      role="alertdialog"
      aria-label={`Delete ${upload.original_filename}?`}
      aria-describedby={warningId}
      className="flex flex-col gap-3 rounded-md border border-red-300 bg-red-50 p-3 dark:border-red-800 dark:bg-red-950"
    >
      <FormError message={formError} />
      <p id={warningId} className="text-sm">
        Delete <strong className="break-all">{upload.original_filename}</strong>? {consequence}
      </p>
      <div className="flex gap-2">
        <Button variant="danger" onClick={handleDelete} disabled={deleteUpload.isPending}>
          {deleteUpload.isPending ? "Deleting…" : "Delete upload"}
        </Button>
        <Button variant="secondary" ref={cancelRef} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
