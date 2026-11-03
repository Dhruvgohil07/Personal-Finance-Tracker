"use client";

// One transaction in the list. On a phone it is a card; from the `md`
// breakpoint the same element is a grid row, lined up with the column
// headings above the list (TransactionsView), so it reads as a table.
// One set of markup for both, so they can't drift apart.
//
// What it does:
//   - shows the date, description, account, badges and the signed amount;
//   - changes the category: when similar transactions can be recognised
//     (the API's `similar_key`: a person's UPI id or a merchant), it first
//     asks whether to file all of them the same way, then sends ONE
//     request with the answer (decided with Dhruv);
//   - adds, edits or removes the user's own note.
//
// What a save did is reported through `onSaved(message)` to the list,
// which says it above itself: this row may leave a filtered list as soon
// as it is saved. Errors stay here, since the row stays when a save fails.

import { useId, useRef, useState } from "react";

import { Button } from "@/components/button";
import { CONTROL_CLASSES } from "@/components/form-field";
import { FormError } from "@/components/form-error";
import { CategorySelect } from "@/components/transactions/category-select";
import { formatDate } from "@/lib/dates";
import { formErrorsFrom } from "@/lib/form-errors";
import {
  NOT_CATEGORIZED_LABEL,
  isGuess,
  signedAmount,
  useUpdateTransaction,
} from "@/lib/transactions";

function Badge({ children, tone = "neutral", title }) {
  const tones = {
    neutral: "bg-neutral-100 text-neutral-800 dark:bg-neutral-800 dark:text-neutral-200",
    amber: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
    blue: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200",
  };
  return (
    <span title={title} className={`rounded-full px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function TransactionRow({ transaction, accountName, categories, onSaved }) {
  const update = useUpdateTransaction();
  // A category chosen but not saved yet, while the "similar?" question is
  // open. Shown in the drop-down meanwhile, so the choice doesn't jump back.
  const [pendingCategory, setPendingCategory] = useState(null);
  const [editingNote, setEditingNote] = useState(false);
  const [noteDraft, setNoteDraft] = useState("");
  const [formError, setFormError] = useState(null);
  // Blocks a second request while one is on its way (see LoginForm).
  const inFlight = useRef(false);
  const questionId = useId();

  const description = transaction.normalized_description || transaction.raw_description;
  const date = formatDate(transaction.txn_date);
  const categoryName = (id) => categories.find((category) => category.id === id)?.name ?? "";
  // What the learned rule would match, computed by the API from the same
  // code that learns the rule; null: nothing to match, so don't ask.
  const key = transaction.similar_key ?? null;

  async function save(changes, message) {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setFormError(null);
    try {
      await update.mutateAsync({ id: transaction.id, changes });
      setPendingCategory(null);
      onSaved(message);
      return true;
    } catch (error) {
      setFormError(formErrorsFrom(error).formError);
      return false;
    } finally {
      inFlight.current = false;
    }
  }

  function handleCategoryChange(event) {
    // "" is the "Not categorized yet" choice: null tells the API to clear it.
    const chosen = event.target.value || null;
    if (chosen === transaction.category_id) {
      setPendingCategory(null);
      return;
    }
    if (chosen !== null && key !== null) {
      setPendingCategory(chosen); // ask first
    } else {
      save({ category_id: chosen }, filedMessage(chosen));
    }
  }

  async function saveNote(event) {
    event.preventDefault();
    // An empty note removes it (null), as the API documents.
    const notes = noteDraft.trim() || null;
    if (await save({ notes }, notes ? `Note saved on ${description}.` : `Note removed from ${description}.`)) {
      setEditingNote(false);
    }
  }

  // "Filed SWIGGY as Food & Dining." / "SWIGGY is not categorized now."
  function filedMessage(categoryId) {
    return categoryId === null
      ? `${description} is not categorized now.`
      : `Filed ${description} as ${categoryName(categoryId)}.`;
  }

  const shownCategory = pendingCategory ?? transaction.category_id ?? "";
  const isDebit = transaction.direction === "debit";

  return (
    <li className="flex flex-col gap-2 rounded-md border border-neutral-200 p-3 md:grid md:grid-cols-[7rem_1fr_14rem_9rem] md:items-start md:gap-4 md:rounded-none md:border-0 md:border-b md:px-1 dark:border-neutral-800">
      <p className="text-muted text-sm md:pt-2">{date}</p>

      <div className="flex min-w-0 flex-col gap-1">
        {/* break-words: a long narration without spaces (UPI ids) wraps
            instead of widening the page on a phone. */}
        <p className="font-medium break-words" title={transaction.raw_description}>
          {description}
        </p>
        <p className="text-muted text-xs">{accountName}</p>
        <div className="flex flex-wrap gap-1">
          {transaction.category_id === null && pendingCategory === null ? (
            <Badge>{NOT_CATEGORIZED_LABEL}</Badge>
          ) : null}
          {isGuess(transaction) ? (
            <Badge tone="amber" title="Kharcha guessed this from the payment. Check it.">
              Guess
            </Badge>
          ) : null}
          {transaction.is_self_transfer ? (
            <Badge tone="blue" title="Money moved between your own accounts. Not counted as spending or income.">
              Self transfer
            </Badge>
          ) : null}
        </div>
        {editingNote ? (
          <form onSubmit={saveNote} className="flex flex-wrap items-center gap-2">
            <input
              className={`${CONTROL_CLASSES} min-w-0 flex-1 text-sm`}
              aria-label={`Note for ${description}`}
              maxLength={500}
              value={noteDraft}
              onChange={(event) => setNoteDraft(event.target.value)}
              autoFocus
            />
            <Button type="submit" disabled={update.isPending}>
              Save
            </Button>
            <Button variant="secondary" onClick={() => setEditingNote(false)}>
              Cancel
            </Button>
          </form>
        ) : (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            {transaction.notes ? <p className="italic">{transaction.notes}</p> : null}
            <button
              type="button"
              className="text-muted text-xs underline"
              aria-label={`${transaction.notes ? "Edit note" : "Add note"} for ${description}`}
              onClick={() => {
                setNoteDraft(transaction.notes ?? "");
                setEditingNote(true);
              }}
            >
              {transaction.notes ? "Edit note" : "Add note"}
            </button>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <CategorySelect
          aria-label={`Category for ${description} on ${date}`}
          categories={categories}
          emptyLabel={NOT_CATEGORIZED_LABEL}
          value={shownCategory}
          onChange={handleCategoryChange}
          disabled={update.isPending}
          className="text-sm"
        />
        {pendingCategory !== null ? (
          // The "apply to similar?" question. role="group" with a label,
          // so a screen reader announces the question with its buttons.
          <div
            role="group"
            aria-labelledby={questionId}
            className="flex flex-col gap-2 rounded-md bg-neutral-100 p-2 text-sm dark:bg-neutral-900"
          >
            <p id={questionId}>
              File every transaction from <strong className="break-words">{key}</strong> as{" "}
              <strong>{categoryName(pendingCategory)}</strong>?
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                onClick={() =>
                  save(
                    { category_id: pendingCategory, apply_to_similar: true },
                    `Filed every transaction from ${key} as ${categoryName(pendingCategory)}. The others are being updated.`,
                  )
                }
                disabled={update.isPending}
              >
                Yes, all similar
              </Button>
              <Button
                variant="secondary"
                onClick={() => save({ category_id: pendingCategory }, filedMessage(pendingCategory))}
                disabled={update.isPending}
              >
                Just this one
              </Button>
              <Button variant="secondary" onClick={() => setPendingCategory(null)}>
                Cancel
              </Button>
            </div>
          </div>
        ) : null}
        <FormError message={formError} />
      </div>

      <p
        className={`font-medium whitespace-nowrap md:pt-2 md:text-right ${
          isDebit ? "" : "text-green-700 dark:text-green-400"
        }`}
      >
        {/* The sign is a visual cue; the words say it for a screen reader. */}
        <span className="sr-only">{isDebit ? "Money out: " : "Money in: "}</span>
        {signedAmount(transaction)}
      </p>
    </li>
  );
}
