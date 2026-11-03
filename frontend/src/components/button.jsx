// The app's one button, so every form looks and behaves the same way.
//
// type defaults to "button": a plain <button> inside a <form> is a SUBMIT
// button, so forgetting the type would submit the form by accident. Forms
// pass type="submit" on purpose.

const BASE =
  "rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-60";

const VARIANTS = {
  primary:
    "bg-neutral-900 text-white hover:bg-neutral-700 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300",
  secondary:
    "border border-neutral-300 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-900",
  danger: "bg-red-700 text-white hover:bg-red-600",
};

export function Button({ variant = "primary", type = "button", className = "", ...props }) {
  return (
    <button type={type} className={`${BASE} ${VARIANTS[variant]} ${className}`} {...props} />
  );
}
