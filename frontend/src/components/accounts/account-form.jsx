"use client";

// The "add an account" form, used on the accounts page and by the first-run
// card on the home page.

import { useRef, useState } from "react";

import { Button } from "@/components/button";
import { FormError } from "@/components/form-error";
import { FormField } from "@/components/form-field";
import {
  ACCOUNT_TYPE_OPTIONS,
  BANK_OPTIONS,
  LAST_FOUR_PATTERN,
  useCreateAccount,
} from "@/lib/accounts";
import { formErrorsFrom } from "@/lib/form-errors";

const FIELDS = ["bank_code", "nickname", "account_type", "masked_number"];
const NO_ERRORS = { fieldErrors: {}, formError: null };
const EMPTY = { bank_code: "ICICI", nickname: "", account_type: "savings", masked_number: "" };

export function AccountForm({ submitLabel = "Add account", onCreated }) {
  const createAccount = useCreateAccount();
  // One state object for the four fields; `update` changes one of them.
  const [values, setValues] = useState(EMPTY);
  const [errors, setErrors] = useState(NO_ERRORS);
  // Blocks a second submit while one is on its way (see LoginForm).
  const inFlight = useRef(false);

  function update(field) {
    return (event) => setValues((current) => ({ ...current, [field]: event.target.value }));
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (inFlight.current) {
      return;
    }

    // Checked here, not only by the input's `pattern`: a full account number
    // must never leave the browser, and `pattern` can be bypassed (it isn't
    // enforced when a form is submitted from code).
    const lastFour = values.masked_number.trim();
    if (!LAST_FOUR_PATTERN.test(lastFour)) {
      setErrors({
        fieldErrors: { masked_number: "Enter exactly the last 4 digits, nothing more." },
        formError: null,
      });
      return;
    }

    inFlight.current = true;
    setErrors(NO_ERRORS);
    try {
      const account = await createAccount.mutateAsync({
        ...values,
        nickname: values.nickname.trim(),
        masked_number: lastFour,
      });
      setValues(EMPTY);
      onCreated?.(account);
    } catch (error) {
      setErrors(formErrorsFrom(error, FIELDS));
    } finally {
      inFlight.current = false;
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <FormError message={errors.formError} />
      <FormField
        label="Bank"
        name="bank_code"
        options={BANK_OPTIONS}
        value={values.bank_code}
        onChange={update("bank_code")}
        hint="ICICI statements are read automatically. For other banks, upload a CSV export and tell Kharcha which column is which."
        error={errors.fieldErrors.bank_code}
      />
      <FormField
        label="Nickname"
        name="nickname"
        required
        maxLength={50}
        placeholder="e.g. Salary account"
        value={values.nickname}
        onChange={update("nickname")}
        error={errors.fieldErrors.nickname}
      />
      <FormField
        label="Account type"
        name="account_type"
        options={ACCOUNT_TYPE_OPTIONS}
        value={values.account_type}
        onChange={update("account_type")}
        error={errors.fieldErrors.account_type}
      />
      <FormField
        label="Last 4 digits of the account number"
        name="masked_number"
        required
        inputMode="numeric"
        pattern="[0-9]{4}"
        autoComplete="off"
        value={values.masked_number}
        onChange={update("masked_number")}
        hint="Only these 4 digits are stored, to tell your accounts apart. Never type the full number."
        error={errors.fieldErrors.masked_number}
      />
      <Button type="submit" disabled={createAccount.isPending}>
        {createAccount.isPending ? "Adding…" : submitLabel}
      </Button>
    </form>
  );
}
