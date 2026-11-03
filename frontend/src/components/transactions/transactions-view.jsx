"use client";

// The transactions page: the filters, then the matching transactions,
// newest first, 50 at a time with "Load more" (decided with Dhruv: no date
// range by default).
//
// The filters come FROM the URL (useSearchParams) and applying them goes
// TO the URL (router.push), never into state of their own; see
// src/lib/transactions.js for why. push, not replace: each applied filter
// is a step the browser's Back button can return to.

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";

import { Button } from "@/components/button";
import { FormError } from "@/components/form-error";
import { TransactionFilters } from "@/components/transactions/transaction-filters";
import { TransactionRow } from "@/components/transactions/transaction-row";
import { useAccounts } from "@/lib/accounts";
import { formErrorsFrom } from "@/lib/form-errors";
import {
  emptyFilters,
  filtersErrorMessage,
  filtersFromSearchParams,
  hasFilters,
  searchParamsFromFilters,
  useCategories,
  useTransactions,
} from "@/lib/transactions";

export function TransactionsView() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  // useMemo: the same filters object while the URL is unchanged, so the
  // query key (which contains it) stays the same between renders.
  const filters = useMemo(() => filtersFromSearchParams(searchParams), [searchParams]);
  const queryString = searchParamsFromFilters(filters).toString();

  const accounts = useAccounts();
  const categories = useCategories();
  const transactions = useTransactions(filters);
  // What the last change did ("Filed … as Groceries."), said ABOVE the list
  // rather than in the row: in a filtered list ("Not categorized yet") the
  // changed row leaves the list at once, and a message inside it would go
  // with it, unseen and unannounced (review of PR 37).
  const [announcement, setAnnouncement] = useState(null);

  function applyFilters(next) {
    setAnnouncement(null);
    const query = searchParamsFromFilters(next).toString();
    router.push(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }

  const nicknames = new Map((accounts.data ?? []).map((account) => [account.id, account.nickname]));
  const items = transactions.data?.pages.flatMap((page) => page.items) ?? [];
  // The rows wait for the categories: each row's drop-down shows its
  // category by id, and without the list of categories the browser would
  // show every row as "Not categorized yet" (review of PR 37).
  const ready = Boolean(transactions.data && categories.data);
  const setupError = accounts.error ?? categories.error;

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">Transactions</h1>

      {setupError ? <FormError message={formErrorsFrom(setupError).formError} /> : null}
      {accounts.data && categories.data ? (
        // key: a new form when the URL's filters change by themselves
        // (Back button), so its draft starts from them.
        <TransactionFilters
          key={queryString}
          filters={filters}
          accounts={accounts.data}
          categories={categories.data}
          onApply={applyFilters}
        />
      ) : null}

      <section aria-label="Transactions list" className="flex flex-col gap-3">
        {/* Always in the page, so screen readers announce what is put in
            it; empty until something is saved. */}
        <p role="status" className="text-sm">
          {announcement}
        </p>
        {transactions.isPending || (transactions.data && categories.isPending) ? (
          <p>Loading transactions…</p>
        ) : null}
        {transactions.error ? (
          <FormError message={filtersErrorMessage(transactions.error)} />
        ) : null}
        {ready && items.length === 0 ? (
          <EmptyState filtered={hasFilters(filters)} onClear={() => applyFilters(emptyFilters())} />
        ) : null}

        {ready && items.length > 0 ? (
          <>
            {/* Column headings for the table layout; a phone shows cards,
                so they are hidden there. aria-hidden: each row says what
                its parts are itself (labels, sr-only text). */}
            <div
              aria-hidden="true"
              className="text-muted hidden border-b border-neutral-200 px-1 pb-2 text-xs font-medium md:grid md:grid-cols-[7rem_1fr_14rem_9rem] md:gap-4 dark:border-neutral-800"
            >
              <span>Date</span>
              <span>Description</span>
              <span>Category</span>
              <span className="text-right">Amount</span>
            </div>
            <ul className="flex flex-col gap-3 md:gap-0">
              {items.map((transaction) => (
                <TransactionRow
                  key={transaction.id}
                  transaction={transaction}
                  accountName={nicknames.get(transaction.account_id) ?? ""}
                  categories={categories.data}
                  onSaved={setAnnouncement}
                />
              ))}
            </ul>
            <p className="text-muted text-sm">
              Showing {items.length} {items.length === 1 ? "transaction" : "transactions"}
              {transactions.hasNextPage ? "; there are more" : ""}.
            </p>
            {transactions.hasNextPage ? (
              <div>
                <Button
                  variant="secondary"
                  onClick={() => transactions.fetchNextPage()}
                  disabled={transactions.isFetchingNextPage}
                >
                  {transactions.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              </div>
            ) : null}
          </>
        ) : null}
      </section>
    </div>
  );
}

function EmptyState({ filtered, onClear }) {
  if (filtered) {
    return (
      <div className="flex flex-col items-start gap-2">
        <p className="text-muted">No transactions match these filters.</p>
        <Button variant="secondary" onClick={onClear}>
          Clear filters
        </Button>
      </div>
    );
  }
  return (
    <p className="text-muted">
      No transactions yet.{" "}
      <Link href="/uploads" className="underline">
        Upload a statement
      </Link>{" "}
      and they will appear here.
    </p>
  );
}
