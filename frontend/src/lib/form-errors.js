// Turns an error from the API into what a form shows: a message under each
// field that was wrong, and/or one message for the whole form.

import { ApiError } from "@/lib/api";

const NETWORK_MESSAGE = "We couldn't reach Kharcha. Check your connection and try again.";

// The backend names a field by where it was found: "body.email".
const BODY_PREFIX = "body.";

function rateLimitMessage(seconds) {
  if (seconds === null) {
    return "Too many attempts. Please wait a minute and try again.";
  }
  const unit = seconds === 1 ? "second" : "seconds";
  return `Too many attempts. Try again in ${seconds} ${unit}.`;
}

/**
 * `{ fieldErrors: { email: "…" }, formError: "…" | null }` for any error.
 *
 * - 422 VALIDATION_ERROR: each problem goes under its field. A problem that
 *   isn't about a field the form shows becomes the form's message.
 * - 429: how long to wait, from the Retry-After header.
 * - any other API error: its message, which the backend keeps safe to show
 *   (e.g. the generic "Invalid email or password.").
 * - not an API error at all (fetch threw: offline, server down): a network
 *   message.
 *
 * `fields` lists the form's own field names, so a field the form doesn't
 * show is never silently dropped.
 */
export function formErrorsFrom(error, fields = []) {
  if (!(error instanceof ApiError)) {
    return { fieldErrors: {}, formError: NETWORK_MESSAGE };
  }

  if (error.status === 429) {
    return { fieldErrors: {}, formError: rateLimitMessage(error.retryAfterSeconds) };
  }

  if (error.code === "VALIDATION_ERROR" && Array.isArray(error.details)) {
    const fieldErrors = {};
    let formError = null;
    for (const problem of error.details) {
      const field = problem.field?.startsWith(BODY_PREFIX)
        ? problem.field.slice(BODY_PREFIX.length)
        : null;
      if (field !== null && fields.includes(field)) {
        // Only the first problem per field: one clear message is enough.
        fieldErrors[field] ??= problem.message;
      } else {
        formError = error.message;
      }
    }
    return { fieldErrors, formError };
  }

  return { fieldErrors: {}, formError: error.message };
}
