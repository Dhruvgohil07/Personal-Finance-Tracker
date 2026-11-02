// One labelled input with the error message for that field.
//
// Accessibility: the <label> is tied to the input with htmlFor/id (clicking
// the label focuses the input, and screen readers read the label), and the
// error is announced through aria-describedby / aria-invalid.

import { useId } from "react";

export function FormField({ label, error, ...inputProps }) {
  // useId: a unique id per component, the same on server and browser.
  const id = useId();
  const errorId = `${id}-error`;

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
        className="rounded-md border border-neutral-300 bg-background px-3 py-2 outline-none focus:border-neutral-500 aria-[invalid]:border-red-600 dark:border-neutral-700"
        {...inputProps}
      />
      {error ? (
        <p id={errorId} className="text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      ) : null}
    </div>
  );
}
