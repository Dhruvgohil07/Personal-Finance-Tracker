// Transactions: the filters (kept in the page's URL), the hooks that list
// and change transactions, the category list, and the small rules the
// list shows (badges, signed amounts, "similar" transactions).
//
// Why the filters live in the URL (Phase 3 plan, Step 8): a filtered view
// can be bookmarked, shared, reloaded and gone back to with the browser's
// Back button, and there is one source of truth for "what is shown" - the
// address bar - instead of state that would be lost on reload.

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formErrorsFrom, readableMessage } from "@/lib/form-errors";
import { formatInr } from "@/lib/money";

export const TRANSACTIONS_QUERY_KEY = ["transactions"];
export const CATEGORIES_QUERY_KEY = ["categories"];

// The value of the category filter that means "no category yet". It goes
// to the API as `uncategorized=true` (decided with Dhruv), never as a
// category_id.
export const UNCATEGORIZED = "uncategorized";

// What "no category" (category_id null) is CALLED on screen: the same name
// the insights API gives that bucket. Not "Uncategorized": the seed has a
// real system category of that name, the user's explicit "I looked and
// nothing fits" (app/schemas/insights.py), and two "Uncategorized" choices
// meaning different things would be indistinguishable.
export const NOT_CATEGORIZED_LABEL = "Not categorized yet";

// Each filter: its name in the URL, and what a valid value looks like.
// A value that doesn't fit (a hand-edited or old link) is dropped rather
// than sent, so a broken link shows everything instead of an error loop.
// Ranges the API checks itself ("from" after "to") are still sent, and
// its 422 is shown.
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Rupees with up to 2 decimals, as the API takes them (never a float).
const RUPEES = /^\d+(\.\d{1,2})?$/;

const FILTERS = {
  from: (value) => ISO_DATE.test(value),
  to: (value) => ISO_DATE.test(value),
  account_id: (value) => UUID.test(value),
  category: (value) => value === UNCATEGORIZED || UUID.test(value),
  direction: (value) => value === "debit" || value === "credit",
  q: (value) => value.trim() !== "" && value.length <= 100,
  min_amount: (value) => RUPEES.test(value),
  max_amount: (value) => RUPEES.test(value),
};

export const FILTER_NAMES = Object.keys(FILTERS);

/** `{ from: "", …, q: "" }`: every filter, unset. */
export function emptyFilters() {
  return Object.fromEntries(FILTER_NAMES.map((name) => [name, ""]));
}

/**
 * The filters in a URL query (URLSearchParams), each as a string, "" when
 * unset or not valid.
 */
export function filtersFromSearchParams(searchParams) {
  const filters = emptyFilters();
  for (const name of FILTER_NAMES) {
    const value = (searchParams.get(name) ?? "").trim();
    if (value !== "" && FILTERS[name](value)) {
      filters[name] = value;
    }
  }
  return filters;
}

/** The URL query for `filters`, set ones only, in a stable order. */
export function searchParamsFromFilters(filters) {
  const params = new URLSearchParams();
  for (const name of FILTER_NAMES) {
    const value = (filters[name] ?? "").trim();
    if (value !== "") {
      params.set(name, value);
    }
  }
  return params;
}

/** True when any filter is set. */
export function hasFilters(filters) {
  return FILTER_NAMES.some((name) => filters[name] !== "");
}

/**
 * The API path for one page: the URL's filters in the API's spelling.
 * The only differences: `category` becomes `category_id`, or
 * `uncategorized=true` for the "no category" choice; and the cursor.
 */
export function transactionsPath(filters, cursor = null) {
  const params = new URLSearchParams();
  for (const name of FILTER_NAMES) {
    const value = filters[name];
    if (value === "") {
      continue;
    }
    if (name === "category") {
      if (value === UNCATEGORIZED) {
        params.set("uncategorized", "true");
      } else {
        params.set("category_id", value);
      }
    } else {
      params.set(name, value);
    }
  }
  if (cursor) {
    params.set("cursor", cursor);
  }
  const query = params.toString();
  return query ? `/transactions?${query}` : "/transactions";
}

// The filters' names on screen, for the API's `query.<name>` error fields.
const FILTER_LABELS = {
  from: "From",
  to: "To",
  account_id: "Account",
  category_id: "Category",
  uncategorized: "Category",
  direction: "Money in or out",
  q: "Search",
  min_amount: "Smallest amount",
  max_amount: "Largest amount",
};

/**
 * What to say when the list request fails.
 *
 * A 422's top-level message is always the generic "The request contains
 * invalid data."; the reason is in `details` (review of PR 37). A rule
 * about the filters together comes as field `query` ("'from' must not be
 * later than 'to'"), a problem with one filter as `query.<name>`, which is
 * prefixed with that filter's label. Anything else: the usual message.
 */
export function filtersErrorMessage(error) {
  const details = error instanceof ApiError && error.code === "VALIDATION_ERROR" ? error.details : null;
  if (!Array.isArray(details) || details.length === 0) {
    return formErrorsFrom(error).formError;
  }
  return details
    .map(({ field, message }) => {
      const label = FILTER_LABELS[(field ?? "").replace(/^query\./, "")];
      const reason = readableMessage(message);
      return label ? `${label}: ${reason}` : reason;
    })
    .join(" ");
}

// ---- what a row shows --------------------------------------------------

/**
 * "−₹70.00" for money out, "+₹500.00" for money in. The amount is always
 * positive in the API and `direction` carries the sign (ADR 001); only
 * formatting happens here, never arithmetic (hard constraint 4).
 */
export function signedAmount(transaction) {
  const sign = transaction.direction === "debit" ? "−" : "+";
  return `${sign}${formatInr(transaction.amount_paise)}`;
}

// The plan's "this is a guess" rule: rows filed by the person-to-person
// heuristic (SPEC §8.3) with low confidence. Other automatic sources
// (a known merchant, a keyword) are reliable enough not to be flagged.
const GUESS_CONFIDENCE = 0.8;

export function isGuess(transaction) {
  return (
    transaction.category_source === "heuristic" &&
    transaction.category_confidence !== null &&
    transaction.category_confidence <= GUESS_CONFIDENCE
  );
}

// What "similar transactions" are recognised by is the API's
// `similar_key` on each row, computed from the rule-learning code itself;
// the browser used to guess it and got it wrong (review of PR 37).

// ---- hooks ---------------------------------------------------------------

/**
 * The transactions matching `filters`, page by page ("Load more").
 *
 * useInfiniteQuery is useQuery for a list that grows a page at a time: it
 * keeps every page fetched so far (`data.pages`), and `fetchNextPage()`
 * asks for the next one with the cursor the last page returned. The
 * filters are part of the query key, so other filters are another list,
 * cached on its own.
 */
export function useTransactions(filters) {
  const { status, client } = useAuth();
  return useInfiniteQuery({
    queryKey: [...TRANSACTIONS_QUERY_KEY, filters],
    queryFn: ({ pageParam, signal }) =>
      client.request(transactionsPath(filters, pageParam), { signal }),
    initialPageParam: null,
    // `next_cursor` is null on the last page; undefined tells TanStack
    // Query there is no next page.
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: status === "authenticated",
  });
}

/** Every category the user can choose: system ones and their own. */
export function useCategories() {
  const { status, client } = useAuth();
  return useQuery({
    queryKey: CATEGORIES_QUERY_KEY,
    queryFn: ({ signal }) => client.request("/categories", { signal }),
    enabled: status === "authenticated",
    // Categories change rarely (custom ones arrive in Phase 4).
    staleTime: 5 * 60 * 1000,
  });
}

// After "apply to similar", the other rows are re-filed by a background
// job (SPEC §8.1) that usually takes well under a second but can wait
// behind an import. So the lists are refetched after each of these delays
// (15 s in all) until the job's work shows up, rather than once after a
// guessed 2 s (review of PR 37).
export const SIMILAR_REFRESH_DELAYS_MS = [1000, 2000, 4000, 8000];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Refetch, wait, refetch… until `settled()` says the job has run, or the
 * delays run out. Resolves to whether it settled. `wait` is replaceable so
 * the tests needn't sleep.
 */
export async function refreshUntilSettled({
  refetch,
  settled,
  delays = SIMILAR_REFRESH_DELAYS_MS,
  wait = sleep,
}) {
  for (const delay of delays) {
    await wait(delay);
    await refetch();
    if (settled()) {
      return true;
    }
  }
  return false;
}

/**
 * True when no cached row still waits for the rule: no row with the same
 * `similar_key` and another category, except rows the user filed by hand,
 * which a rule never changes.
 */
export function similarRowsSettled(lists, similarKey, categoryId) {
  return !lists
    .flatMap((data) => data?.pages.flatMap((page) => page.items) ?? [])
    .some(
      (row) =>
        row.similar_key === similarKey &&
        row.category_source !== "user_manual" &&
        row.category_id !== categoryId,
    );
}

/**
 * Change one transaction: `mutateAsync({ id, changes })`, where `changes`
 * holds any of `category_id` (null = not categorized), `notes` (null =
 * remove) and `apply_to_similar`.
 *
 * Afterwards every cached transaction list is fetched again: the row may
 * no longer match the filters (it left "Not categorized yet"), and with
 * `apply_to_similar` other rows change too, so the lists are refetched
 * again until those rows show the new category (refreshUntilSettled).
 */
export function useUpdateTransaction() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, changes }) =>
      client.request(`/transactions/${id}`, { method: "PATCH", body: changes }),
    onSuccess: (updated, { changes }) => {
      // The answer is the changed row: show it at once in every cached
      // list, then refetch them, which can take a moment with many pages.
      queryClient.setQueriesData({ queryKey: TRANSACTIONS_QUERY_KEY }, (data) =>
        data
          ? {
              ...data,
              pages: data.pages.map((page) => ({
                ...page,
                items: page.items.map((item) => (item.id === updated.id ? updated : item)),
              })),
            }
          : data,
      );
      queryClient.invalidateQueries({ queryKey: TRANSACTIONS_QUERY_KEY });
      if (changes.apply_to_similar) {
        // Not awaited: the save is done; this only brings in what the job
        // does next. Returning it would make mutateAsync wait up to 15 s.
        refreshUntilSettled({
          refetch: () => queryClient.refetchQueries({ queryKey: TRANSACTIONS_QUERY_KEY }),
          settled: () =>
            similarRowsSettled(
              queryClient.getQueriesData({ queryKey: TRANSACTIONS_QUERY_KEY }).map(([, d]) => d),
              updated.similar_key,
              changes.category_id,
            ),
        });
      }
    },
  });
}
