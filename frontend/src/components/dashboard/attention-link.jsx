// "3 transactions in October need a category": the month's transactions
// that nobody has filed yet, money in and out, linking to exactly those
// on the transactions page. The count is the summary's
// `uncategorized_count` (decided with Dhruv: not the donut's bucket, which
// counts money out only).
//
// "Guess" rows are not counted: they have a category, and the
// transactions page marks them itself.

import Link from "next/link";

import { monthName, uncategorizedTransactionsLink } from "@/lib/insights";
import { plural } from "@/lib/text";

export function AttentionLink({ count, month }) {
  if (count === 0) {
    return null;
  }
  return (
    <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100">
      {plural(count, "transaction")} in {monthName(month)} {count === 1 ? "needs" : "need"} your
      attention: {count === 1 ? "it has" : "they have"} no category yet.{" "}
      <Link href={uncategorizedTransactionsLink(month)} className="font-medium underline">
        Sort {count === 1 ? "it" : "them"} now
      </Link>
    </p>
  );
}
