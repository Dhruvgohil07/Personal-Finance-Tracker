"use client";

// The filters above the transactions list. They are edited as a DRAFT in
// this form's state and only take effect on "Apply" (or a quick button):
// applying puts them in the URL, and the list reads them from there. Typing
// in the search box therefore doesn't send a request per keystroke.
//
// The parent gives this form a `key` made from the URL's query, so when
// the URL changes by itself (the browser's Back button), the form is
// re-created with the filters now in the URL instead of keeping a stale
// draft.

import { useId, useState } from "react";

import { Button } from "@/components/button";
import { FormField } from "@/components/form-field";
import { CategorySelect } from "@/components/transactions/category-select";
import { monthRange, todayInIndia } from "@/lib/dates";
import { NOT_CATEGORIZED_LABEL, UNCATEGORIZED, emptyFilters } from "@/lib/transactions";

const DIRECTION_OPTIONS = [
  { value: "", label: "Money in and out" },
  { value: "debit", label: "Money out" },
  { value: "credit", label: "Money in" },
];

export function TransactionFilters({ filters, accounts, categories, onApply }) {
  const [draft, setDraft] = useState(filters);
  const categoryId = useId();

  function update(name) {
    return (event) => setDraft((current) => ({ ...current, [name]: event.target.value }));
  }

  function handleSubmit(event) {
    event.preventDefault();
    onApply(draft);
  }

  // "This month" / "Last month": the calendar month in India's time.
  function applyMonth(offset) {
    onApply({ ...draft, ...monthRange(todayInIndia(), offset) });
  }

  const accountOptions = [
    { value: "", label: "All accounts" },
    ...accounts.map((account) => ({
      value: account.id,
      label: `${account.nickname} (•••• ${account.masked_number})`,
    })),
  ];

  return (
    <form
      onSubmit={handleSubmit}
      aria-label="Filter transactions"
      className="flex flex-col gap-4 rounded-md border border-neutral-200 p-4 dark:border-neutral-800"
    >
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" onClick={() => applyMonth(0)}>
          This month
        </Button>
        <Button variant="secondary" onClick={() => applyMonth(-1)}>
          Last month
        </Button>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <FormField label="From" name="from" type="date" value={draft.from} onChange={update("from")} />
        <FormField label="To" name="to" type="date" value={draft.to} onChange={update("to")} />
        <FormField
          label="Account"
          name="account_id"
          options={accountOptions}
          value={draft.account_id}
          onChange={update("account_id")}
        />
        <div className="flex flex-col gap-1">
          <label htmlFor={categoryId} className="text-sm font-medium">
            Category
          </label>
          <CategorySelect
            id={categoryId}
            name="category"
            categories={categories}
            emptyLabel="All categories"
            extraOptions={[{ value: UNCATEGORIZED, label: NOT_CATEGORIZED_LABEL }]}
            value={draft.category}
            onChange={update("category")}
          />
        </div>
        <FormField
          label="Money in or out"
          name="direction"
          options={DIRECTION_OPTIONS}
          value={draft.direction}
          onChange={update("direction")}
        />
        <FormField
          label="Search"
          name="q"
          type="search"
          maxLength={100}
          placeholder="e.g. swiggy"
          value={draft.q}
          onChange={update("q")}
        />
        <FormField
          label="Smallest amount (₹)"
          name="min_amount"
          inputMode="decimal"
          pattern="\d+(\.\d{1,2})?"
          value={draft.min_amount}
          onChange={update("min_amount")}
        />
        <FormField
          label="Largest amount (₹)"
          name="max_amount"
          inputMode="decimal"
          pattern="\d+(\.\d{1,2})?"
          value={draft.max_amount}
          onChange={update("max_amount")}
        />
      </div>
      <div className="flex gap-2">
        <Button type="submit">Apply filters</Button>
        <Button variant="secondary" onClick={() => onApply(emptyFilters())}>
          Clear
        </Button>
      </div>
    </form>
  );
}
