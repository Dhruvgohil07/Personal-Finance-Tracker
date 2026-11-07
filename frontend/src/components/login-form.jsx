"use client";

// The login form. On success it only starts the session; the (auth)
// layout's RedirectIfSignedIn then takes the user on.
//
// Right after an account was deleted (Settings), the guard brings the
// browser here, and the form says the deletion is done. The flag lives in
// the session context, not the URL, so a reload doesn't say it again.

import Link from "next/link";
import { useRef, useState } from "react";

import { Button } from "@/components/button";
import { FormError } from "@/components/form-error";
import { FormField } from "@/components/form-field";
import { useAuth } from "@/lib/auth";
import { formErrorsFrom } from "@/lib/form-errors";

const FIELDS = ["email", "password"];
const NO_ERRORS = { fieldErrors: {}, formError: null };

export function LoginForm() {
  const { login, accountDeleted } = useAuth();
  // "Controlled" inputs: React state holds each value, and the input shows
  // what the state says.
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState(NO_ERRORS);
  const [submitting, setSubmitting] = useState(false);
  // Blocks a second submit (a double click, Enter pressed twice) while one
  // is on its way. `submitting` can't do it: a state change only shows in
  // the NEXT render, so a second submit in the same instant still sees
  // false. A ref changes at once (review of PR 31).
  const inFlight = useRef(false);

  async function handleSubmit(event) {
    // A form would otherwise reload the page with the values in the URL.
    event.preventDefault();
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setSubmitting(true);
    setErrors(NO_ERRORS);
    try {
      await login(email, password);
    } catch (error) {
      setErrors(formErrorsFrom(error, FIELDS));
      setSubmitting(false);
      inFlight.current = false;
    }
    // On success the button stays disabled until the redirect happens.
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold">Log in</h1>
      {accountDeleted ? (
        // role="status": announced by screen readers, without the urgency
        // of an alert (nothing went wrong).
        <p
          role="status"
          className="rounded-md border border-neutral-300 px-3 py-2 text-sm dark:border-neutral-700"
        >
          Your account and all its data have been deleted.
        </p>
      ) : null}
      <FormError message={errors.formError} />
      <FormField
        label="Email"
        type="email"
        name="email"
        autoComplete="email"
        required
        value={email}
        onChange={(event) => setEmail(event.target.value)}
        error={errors.fieldErrors.email}
      />
      <FormField
        label="Password"
        type="password"
        name="password"
        autoComplete="current-password"
        required
        maxLength={128}
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        error={errors.fieldErrors.password}
      />
      <Button type="submit" disabled={submitting}>
        {submitting ? "Logging in…" : "Log in"}
      </Button>
      <p className="text-muted text-sm">
        New here?{" "}
        <Link href="/register" className="underline">
          Create an account
        </Link>
      </p>
    </form>
  );
}
