// Small wording helpers shared by the pages.

/** "1 transaction", "0 transactions", "3 minutes": a count with its noun. */
export function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}
