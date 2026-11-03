// One labelled input (or drop-down) with a hint and the field's error.
//
// Accessibility: the <label> is tied to the input with htmlFor/id (clicking
// the label focuses the input, and screen readers read the label), and the
// hint and error are announced through aria-describedby / aria-invalid.
//
// Pass `options` ([{ value, label }]) for a <select>; otherwise an <input>.

import { useId } from "react";

// Exported for controls FormField doesn't draw itself (a <select> with
// <optgroup>s, see CategorySelect), so every control looks the same.
export const CONTROL_CLASSES =
  "rounded-md border border-neutral-300 bg-background px-3 py-2 outline-none focus:border-neutral-500 aria-[invalid]:border-red-600 dark:border-neutral-700";

export function FormField({ label, hint, error, options, ...controlProps }) {
  // useId: a unique id per component, the same on server and browser.
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  // The ids of the texts that describe the field, if any.
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ");

  const shared = {
    id,
    className: CONTROL_CLASSES,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": describedBy || undefined,
    ...controlProps,
  };

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {options ? (
        <select {...shared}>
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      ) : (
        <input {...shared} />
      )}
      {hint ? (
        <p id={hintId} className="text-muted text-xs">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      ) : null}
    </div>
  );
}
