import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { formErrorsFrom } from "@/lib/form-errors";

const FIELDS = ["email", "password"];

function validationError(details) {
  return new ApiError({
    status: 422,
    code: "VALIDATION_ERROR",
    message: "The request contains invalid data.",
    details,
  });
}

describe("formErrorsFrom", () => {
  it("puts each validation problem under its field", () => {
    const error = validationError([
      { field: "body.email", message: "value is not a valid email address", type: "value_error" },
      { field: "body.password", message: "String should have at least 8 characters", type: "x" },
    ]);

    expect(formErrorsFrom(error, FIELDS)).toEqual({
      fieldErrors: {
        email: "value is not a valid email address",
        password: "String should have at least 8 characters",
      },
      formError: null,
    });
  });

  it("keeps only the first problem of a field", () => {
    const error = validationError([
      { field: "body.password", message: "first", type: "x" },
      { field: "body.password", message: "second", type: "x" },
    ]);

    expect(formErrorsFrom(error, FIELDS).fieldErrors.password).toBe("first");
  });

  it("shows a problem with a field the form doesn't have as the form's message", () => {
    // e.g. an unknown field ("extra_forbidden"), or a missing body
    const error = validationError([{ field: "body.nickname", message: "Extra inputs", type: "x" }]);

    expect(formErrorsFrom(error, FIELDS)).toEqual({
      fieldErrors: {},
      formError: "The request contains invalid data.",
    });
  });

  it.each([
    [42, "Too many attempts. Try again in 42 seconds."],
    [1, "Too many attempts. Try again in 1 second."],
    [60, "Too many attempts. Try again in 60 seconds."],
    // Above a minute: whole minutes, rounded up (the upload limit is per hour).
    [61, "Too many attempts. Try again in 2 minutes."],
    [3412, "Too many attempts. Try again in 57 minutes."],
    [null, "Too many attempts. Please wait a minute and try again."],
  ])("tells how long to wait after a 429 (Retry-After %j)", (seconds, message) => {
    const error = new ApiError({
      status: 429,
      code: "RATE_LIMITED",
      message: "Too many requests. Please try again later.",
      retryAfterSeconds: seconds,
    });

    expect(formErrorsFrom(error, FIELDS)).toEqual({ fieldErrors: {}, formError: message });
  });

  it("shows any other API error's own message", () => {
    const error = new ApiError({
      status: 401,
      code: "UNAUTHORIZED",
      message: "Invalid email or password.",
    });

    expect(formErrorsFrom(error, FIELDS).formError).toBe("Invalid email or password.");
  });

  it("explains a network failure (fetch threw)", () => {
    expect(formErrorsFrom(new TypeError("Failed to fetch"), FIELDS).formError).toMatch(
      /couldn't reach Kharcha/,
    );
  });
});
