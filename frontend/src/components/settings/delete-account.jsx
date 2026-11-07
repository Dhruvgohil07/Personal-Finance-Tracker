"use client";

// "Delete account": everything Kharcha holds about you, gone for good
// (DELETE /me, ADR 021). Two steps, so it can't happen by accident:
//
// 1. "Delete my account…" opens the confirmation (the "…" says that a
//    question follows, not the deletion itself);
// 2. the confirmation needs the word DELETE typed out (decided with Dhruv:
//    quick to read and the same for everyone) AND the current password,
//    which the API checks, so a session left open on someone else's
//    computer can't be used to wipe the account.
//
// The delete button stays disabled until both are filled in. On success
// the session ends (useAuth().deleteAccount) and the signed-in layout's
// guard sends the browser to /login, which says the account was deleted.

import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/button";
import { FormError } from "@/components/form-error";
import { FormField } from "@/components/form-field";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formErrorsFrom } from "@/lib/form-errors";

export const CONFIRMATION_WORD = "DELETE";

const NO_ERRORS = { fieldErrors: {}, formError: null };

export function DeleteAccount() {
  const [open, setOpen] = useState(false);

  return (
    <section aria-labelledby="delete-heading" className="flex flex-col gap-3">
      <h2 id="delete-heading" className="text-lg font-semibold">
        Delete account
      </h2>
      {open ? (
        <ConfirmDelete onCancel={() => setOpen(false)} />
      ) : (
        <>
          <p className="text-muted text-sm">
            Removes your Kharcha account and everything in it. You will be asked to confirm.
          </p>
          <div>
            <Button variant="danger" onClick={() => setOpen(true)}>
              Delete my account…
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

function ConfirmDelete({ onCancel }) {
  const { deleteAccount } = useAuth();
  const [confirmation, setConfirmation] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState(NO_ERRORS);
  const [submitting, setSubmitting] = useState(false);
  // Blocks a second submit while one is on its way (see LoginForm).
  const inFlight = useRef(false);
  const confirmationRef = useRef(null);
  const warningId = useId();

  // Focus the first thing to fill in. Unlike the account delete (which
  // focuses "Cancel"), a stray Enter can't delete anything here: the
  // button is disabled until the word and the password are typed.
  useEffect(() => {
    confirmationRef.current?.focus();
  }, []);

  const ready = confirmation.trim() === CONFIRMATION_WORD && password !== "";

  async function handleSubmit(event) {
    event.preventDefault();
    if (!ready || inFlight.current) {
      return;
    }
    inFlight.current = true;
    setSubmitting(true);
    setErrors(NO_ERRORS);
    try {
      await deleteAccount(password);
      // Nothing more here: the session has ended, and the guard is
      // already taking the browser to /login. This component is about to
      // disappear, so no state is set on success.
    } catch (error) {
      if (error instanceof ApiError && error.code === "INCORRECT_PASSWORD") {
        // A 400, not a 401 (ADR 021): the session goes on, so the password
        // can be typed again. Cleared, so the wrong one isn't resent.
        setErrors({ fieldErrors: { password: error.message }, formError: null });
        setPassword("");
      } else {
        setErrors(formErrorsFrom(error, ["password"]));
      }
      setSubmitting(false);
      inFlight.current = false;
    }
  }

  return (
    // role="alertdialog" + aria-describedby: a screen reader announces the
    // warning together with the dialog's name (as in the accounts page).
    <form
      role="alertdialog"
      aria-label="Delete your account?"
      aria-describedby={warningId}
      onSubmit={handleSubmit}
      className="flex flex-col gap-4 rounded-md border border-red-300 bg-red-50 p-4 dark:border-red-800 dark:bg-red-950"
    >
      <div id={warningId} className="flex flex-col gap-2 text-sm">
        <p className="font-medium">This permanently deletes:</p>
        <ul className="list-disc pl-5">
          <li>your Kharcha login,</li>
          <li>every account you added,</li>
          <li>every uploaded statement and every transaction from them,</li>
          <li>your categories, notes and rules.</li>
        </ul>
        <p>It can&apos;t be undone, and you will be logged out.</p>
      </div>
      <FormError message={errors.formError} />
      <FormField
        ref={confirmationRef}
        label={`Type ${CONFIRMATION_WORD} to confirm`}
        name="confirmation"
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
        value={confirmation}
        onChange={(event) => setConfirmation(event.target.value)}
      />
      <FormField
        label="Your password"
        type="password"
        name="password"
        // The real login password: let the password manager fill it in.
        autoComplete="current-password"
        maxLength={128}
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        error={errors.fieldErrors.password}
      />
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="danger" disabled={!ready || submitting}>
          {submitting ? "Deleting…" : "Delete my account forever"}
        </Button>
        <Button variant="secondary" onClick={onCancel} disabled={submitting}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
