// The message for a whole form (e.g. "Invalid email or password.").
// role="alert" makes screen readers announce it as soon as it appears.

export function FormError({ message }) {
  if (!message) {
    return null;
  }
  return (
    <p
      role="alert"
      className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200"
    >
      {message}
    </p>
  );
}
