"use client";

// One account in the list, with its "Edit" and "Delete" actions.
//
// The row has three modes: showing the account, editing it, or asking
// whether to delete it. Only one at a time, kept in `mode`.

import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/button";
import { FormError } from "@/components/form-error";
import { FormField } from "@/components/form-field";
import {
  ACCOUNT_TYPE_OPTIONS,
  accountTypeLabel,
  bankLabel,
  useDeleteAccount,
  useUpdateAccount,
} from "@/lib/accounts";
import { formErrorsFrom } from "@/lib/form-errors";

const NO_ERRORS = { fieldErrors: {}, formError: null };

export function AccountRow({ account }) {
  const [mode, setMode] = useState("view"); // "view" | "edit" | "confirm-delete"

  return (
    <li className="rounded-md border border-neutral-200 p-4 dark:border-neutral-800">
      {mode === "edit" ? (
        <EditAccount account={account} onDone={() => setMode("view")} />
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="font-medium">{account.nickname}</p>
            <p className="text-muted text-sm">
              {bankLabel(account.bank_code)} · {accountTypeLabel(account.account_type)} ·{" "}
              <span aria-label={`ending in ${account.masked_number}`}>
                •••• {account.masked_number}
              </span>
            </p>
          </div>
          {mode === "view" ? (
            <div className="flex gap-2">
              {/* aria-label names the account: with several rows, a screen
                  reader would otherwise list "Edit, Delete, Edit, Delete"
                  with nothing saying which account each one is for. The
                  label starts with the visible word, so voice control
                  ("click Delete") still finds it (review of PR 32). */}
              <Button
                variant="secondary"
                aria-label={`Edit ${account.nickname}`}
                onClick={() => setMode("edit")}
              >
                Edit
              </Button>
              <Button
                variant="secondary"
                aria-label={`Delete ${account.nickname}`}
                onClick={() => setMode("confirm-delete")}
              >
                Delete
              </Button>
            </div>
          ) : null}
        </div>
      )}
      {mode === "confirm-delete" ? (
        <ConfirmDelete account={account} onCancel={() => setMode("view")} />
      ) : null}
    </li>
  );
}

function EditAccount({ account, onDone }) {
  const updateAccount = useUpdateAccount();
  const [nickname, setNickname] = useState(account.nickname);
  const [accountType, setAccountType] = useState(account.account_type);
  const [errors, setErrors] = useState(NO_ERRORS);
  // isPending only changes on the next render; the ref blocks a second
  // submit in the same instant (see LoginForm).
  const inFlight = useRef(false);

  async function handleSubmit(event) {
    event.preventDefault();
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setErrors(NO_ERRORS);
    try {
      await updateAccount.mutateAsync({
        id: account.id,
        changes: { nickname: nickname.trim(), account_type: accountType },
      });
      onDone();
    } catch (error) {
      setErrors(formErrorsFrom(error, ["nickname", "account_type"]));
    } finally {
      inFlight.current = false;
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <FormError message={errors.formError} />
      <FormField
        label="Nickname"
        name="nickname"
        required
        maxLength={50}
        value={nickname}
        onChange={(event) => setNickname(event.target.value)}
        error={errors.fieldErrors.nickname}
      />
      <FormField
        label="Account type"
        name="account_type"
        options={ACCOUNT_TYPE_OPTIONS}
        value={accountType}
        onChange={(event) => setAccountType(event.target.value)}
        error={errors.fieldErrors.account_type}
      />
      <p className="text-muted text-xs">
        The bank and the last 4 digits say which real account this is, so they can&apos;t be
        changed. If they are wrong, delete this account and add the right one.
      </p>
      <div className="flex gap-2">
        <Button type="submit" disabled={updateAccount.isPending}>
          {updateAccount.isPending ? "Saving…" : "Save"}
        </Button>
        <Button variant="secondary" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function ConfirmDelete({ account, onCancel }) {
  const deleteAccount = useDeleteAccount();
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
      await deleteAccount.mutateAsync(account.id);
      // Nothing else to do: the list is fetched again and this row is gone.
    } catch (error) {
      setFormError(formErrorsFrom(error).formError);
    } finally {
      inFlight.current = false;
    }
  }

  return (
    // role="alertdialog" + aria-describedby: when focus lands on "Cancel",
    // a screen reader announces the dialog's name AND the warning text.
    // As a plain group it said only "Cancel, button", so a blind user could
    // confirm without ever hearing what gets deleted (review of PR 32).
    <div
      role="alertdialog"
      aria-label={`Delete ${account.nickname}?`}
      aria-describedby={warningId}
      className="mt-3 flex flex-col gap-3 rounded-md border border-red-300 bg-red-50 p-3 dark:border-red-800 dark:bg-red-950"
    >
      <FormError message={formError} />
      <p id={warningId} className="text-sm">
        Delete <strong>{account.nickname}</strong>? This permanently deletes all its statements
        and transactions. It can&apos;t be undone.
      </p>
      <div className="flex gap-2">
        <Button variant="danger" onClick={handleDelete} disabled={deleteAccount.isPending}>
          {deleteAccount.isPending ? "Deleting…" : "Delete account"}
        </Button>
        <Button variant="secondary" ref={cancelRef} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
