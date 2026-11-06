"use client";

// The dashboard (Phase 3 Step 9): one month at a glance.
//
//   ‹ October 2026 ›                       the month, kept in the URL
//   Money in | Money out | Left over       GET /insights/summary
//   "3 transactions need your attention"   its uncategorized_count
//   Where it went: donut + list            GET /insights/categories
//
// The month lives in the URL (?month=2026-10), like the transactions
// page's filters: a reload, a bookmark or the Back button shows the same
// month. Without one, the page opens on the newest transaction's month
// (see chooseMonth in src/lib/insights.js); choosing another month puts
// it in the URL.
//
// Each part has its own loading and error state, so a failing breakdown
// doesn't hide the totals.

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { AttentionLink } from "@/components/dashboard/attention-link";
import { CategoryBreakdown } from "@/components/dashboard/category-breakdown";
import { MonthPicker } from "@/components/dashboard/month-picker";
import { SummaryCards } from "@/components/dashboard/summary-cards";
import { FormError } from "@/components/form-error";
import { formErrorsFrom } from "@/lib/form-errors";
import {
  chooseMonth,
  currentMonth,
  monthLabel,
  urlMonthIsUsable,
  useCategoryBreakdown,
  useNewestTransactionDate,
  useSummary,
} from "@/lib/insights";

export function Dashboard() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const today = currentMonth();
  const urlMonth = searchParams.get("month");
  // The newest transaction is only needed when the URL doesn't say.
  const needsNewest = !urlMonthIsUsable(urlMonth, today);
  const newest = useNewestTransactionDate({ enabled: needsNewest });
  const month =
    needsNewest && !newest.isSuccess
      ? null
      : chooseMonth({ urlMonth, newestDate: newest.data ?? null, today });
  // Known only when the newest transaction was asked for: no transactions
  // at all, e.g. while the first statement is still importing.
  const noTransactionsYet = needsNewest && newest.data === null;

  const summary = useSummary(month);
  const breakdown = useCategoryBreakdown(month);

  function showMonth(next) {
    // push, not replace: each month is a step the Back button returns to.
    router.push(`${pathname}?month=${next}`, { scroll: false });
  }

  if (needsNewest && newest.error) {
    return <FormError message={formErrorsFrom(newest.error).formError} />;
  }
  if (month === null) {
    return <p role="status">Loading your month…</p>;
  }

  return (
    <section aria-label="Your month" className="flex flex-col gap-6">
      <MonthPicker month={month} today={today} onChange={showMonth} />

      {summary.error ? <FormError message={formErrorsFrom(summary.error).formError} /> : null}
      {summary.isPending ? <p role="status">Loading the totals…</p> : null}
      {summary.data ? (
        summary.data.current.transaction_count === 0 ? (
          <EmptyMonth month={month} noTransactionsYet={noTransactionsYet} />
        ) : (
          <>
            <SummaryCards summary={summary.data} />
            <AttentionLink count={summary.data.current.uncategorized_count} month={month} />
            <section aria-labelledby="where-it-went" className="flex flex-col gap-3">
              <h3 id="where-it-went" className="text-lg font-semibold">
                Where it went
              </h3>
              <Breakdown query={breakdown} month={month} />
            </section>
          </>
        )
      ) : null}
    </section>
  );
}

function Breakdown({ query, month }) {
  if (query.error) {
    return <FormError message={formErrorsFrom(query.error).formError} />;
  }
  if (query.isPending) {
    return <p role="status">Loading the categories…</p>;
  }
  if (query.data.items.length === 0) {
    return <p className="text-muted">No money went out in {monthLabel(month)}.</p>;
  }
  return <CategoryBreakdown breakdown={query.data} month={month} />;
}

// A month with no income or spending. Two cases, worded apart (review of
// PR 38):
// - no transactions at all: during the first run the upload above is
//   usually still importing, so asking for an upload would read as if it
//   hadn't counted;
// - otherwise the month may have only self transfers (left out of every
//   total), so "nothing here" would be untrue: say what is counted, and
//   offer the upload only as a question.
function EmptyMonth({ month, noTransactionsYet }) {
  if (noTransactionsYet) {
    return (
      <p className="text-muted">
        Your months appear here once a statement has been imported.
      </p>
    );
  }
  return (
    <p className="text-muted">
      No money came in or went out in {monthLabel(month)} (money moved between your own
      accounts isn&apos;t counted). Missing this month&apos;s statement?{" "}
      <Link href="/uploads" className="underline">
        Upload it
      </Link>
      .
    </p>
  );
}
