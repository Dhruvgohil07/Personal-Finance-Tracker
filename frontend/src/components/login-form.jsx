"use client";

// The login form. On success it only starts the session; the (auth)
// layout's RedirectIfSignedIn then takes the user on.

import Link from "next/link";
import { useState } from "react";

import { FormError } from "@/components/form-error";
import { FormField } from "@/components/form-field";
import { useAuth } from "@/lib/auth";
import { formErrorsFrom } from "@/lib/form-errors";

const FIELDS = ["email", "password"];
const NO_ERRORS = { fieldErrors: {}, formError: null };

export function LoginForm() {
  const { login } = useAuth();
  // "Controlled" inputs: React state holds each value, and the input shows
  // what the state says.
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState(NO_ERRORS);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event) {
    // A form would otherwise reload the page with the values in the URL.
    event.preventDefault();
    setSubmitting(true);
    setErrors(NO_ERRORS);
    try {
      await login(email, password);
    } catch (error) {
      setErrors(formErrorsFrom(error, FIELDS));
      setSubmitting(false);
    }
    // On success the button stays disabled until the redirect happens.
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold">Log in</h1>
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
      <button
        type="submit"
        disabled={submitting}
        className="rounded-md bg-neutral-900 px-4 py-2 font-medium text-white hover:bg-neutral-700 disabled:opacity-60 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300"
      >
        {submitting ? "Logging in…" : "Log in"}
      </button>
      <p className="text-muted text-sm">
        New here?{" "}
        <Link href="/register" className="underline">
          Create an account
        </Link>
      </p>
    </form>
  );
}
