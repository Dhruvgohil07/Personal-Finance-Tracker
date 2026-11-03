"use client";

// The sign-up form: POST /auth/register, then logs in with the same details,
// so the new user doesn't have to type them twice.

import Link from "next/link";
import { useRef, useState } from "react";

import { Button } from "@/components/button";
import { FormError } from "@/components/form-error";
import { FormField } from "@/components/form-field";
import { useAuth } from "@/lib/auth";
import { formErrorsFrom } from "@/lib/form-errors";

const FIELDS = ["name", "email", "password"];
const NO_ERRORS = { fieldErrors: {}, formError: null };

// The backend's rules (app/schemas/auth.py), checked by the browser first.
const MIN_PASSWORD_LENGTH = 8;
const MAX_PASSWORD_LENGTH = 128;

export function RegisterForm() {
  const { login, client } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState(NO_ERRORS);
  const [submitting, setSubmitting] = useState(false);
  // Blocks a second submit while one is on its way; see LoginForm. Here it
  // matters more: a second POST /auth/register would answer 409 "already
  // exists" for the account the first one just created.
  const inFlight = useRef(false);

  async function handleSubmit(event) {
    event.preventDefault();
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setSubmitting(true);
    setErrors(NO_ERRORS);

    try {
      await client.request("/auth/register", {
        method: "POST",
        body: { name, email, password },
      });
    } catch (error) {
      setErrors(formErrorsFrom(error, FIELDS));
      setSubmitting(false);
      inFlight.current = false;
      return;
    }

    try {
      await login(email, password);
    } catch (error) {
      // The account exists now; only the automatic login failed (e.g. the
      // login rate limit). Say so, instead of a confusing generic error.
      const { formError } = formErrorsFrom(error, []);
      setErrors({
        fieldErrors: {},
        formError: `Your account was created, but logging in failed: ${formError} Please log in.`,
      });
      setSubmitting(false);
      inFlight.current = false;
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold">Create an account</h1>
      <FormError message={errors.formError} />
      <FormField
        label="Name"
        name="name"
        autoComplete="name"
        required
        maxLength={100}
        value={name}
        onChange={(event) => setName(event.target.value)}
        error={errors.fieldErrors.name}
      />
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
        label={`Password (at least ${MIN_PASSWORD_LENGTH} characters)`}
        type="password"
        name="password"
        autoComplete="new-password"
        required
        minLength={MIN_PASSWORD_LENGTH}
        maxLength={MAX_PASSWORD_LENGTH}
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        error={errors.fieldErrors.password}
      />
      <Button type="submit" disabled={submitting}>
        {submitting ? "Creating your account…" : "Create account"}
      </Button>
      <p className="text-muted text-sm">
        Already have an account?{" "}
        <Link href="/login" className="underline">
          Log in
        </Link>
      </p>
    </form>
  );
}
