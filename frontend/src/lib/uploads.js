// Statement uploads: the rules the browser checks before sending a file,
// the words for each status, and the hooks that call /uploads.
//
// An upload's life (backend app/models/statement_upload.py):
//
//   POST /uploads ──► pending ──► processing ──► completed
//        │                                  ├──► needs_review (imported, but
//        │                                  │    the balances don't add up)
//        │                                  └──► failed (error_message says why)
//        └─ a locked PDF sent without its password: needs_password, until
//           POST /uploads/{id}/password unlocks it (then pending again).
//
// The import runs in a background worker (ADR 013), so the page POLLS:
// it asks GET /uploads/{id} again every few seconds while the upload is
// pending or processing. How many seconds is the API's choice, sent in the
// `Retry-After` header; the header is absent once there is nothing to wait
// for, and polling stops (Phase 3 plan, design decisions). The polling is
// done by ONE watcher for the whole signed-in app (useUnfinishedUploadsPolling,
// mounted by UploadsWatcher in the layout), not by each row on screen.

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export const UPLOADS_QUERY_KEY = ["uploads"];
// The root of each upload's POLLING query, ["upload", id]. Deliberately a
// different word from the list's "uploads": "refetch everything except the
// polls" can then be said by the first element alone (see pollQuery).
const POLL_QUERY_ROOT = "upload";

// The backend's default MAX_UPLOAD_MB (app/core/config.py), decided with
// Dhruv as a constant here rather than an env variable or an endpoint. The
// backend still enforces its own limit (413); this check exists because a
// file above the Next.js proxy's 12 MB never reaches the backend at all:
// the proxy answers a bare 500 (ADR 022). Keep the two numbers in step.
export const MAX_UPLOAD_MB = 10;
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;

// For the file picker's `accept`: it only pre-filters the dialog (the user
// can still pick "All files"), so the backend's own type check by file
// signature is what really decides.
export const ACCEPTED_FILE_TYPES = ".pdf,.xls,.csv";

// The `autocomplete` of every statement-password field. A statement
// password is NOT a password for this site, so the browser's password
// manager must neither fill it in nor offer to save it. "off" doesn't do
// that: browsers ignore autocomplete="off" on type="password" fields, so
// Chrome could fill in the user's saved Kharcha login (which would be sent
// to the API as the PDF's password) or offer to store the bank's statement
// password (review of PR 33). "one-time-code" marks the field as a code
// typed once; Chromium's password manager leaves such fields alone.
// Rejected: "new-password", which makes Chrome suggest a generated password
// and offer to save what was typed as this site's new password.
// Browser behaviour, so not provable in the jsdom tests: they only check
// that both fields carry this value.
export const STATEMENT_PASSWORD_AUTOCOMPLETE = "one-time-code";

const UNFINISHED_STATUSES = ["pending", "processing"];

export const STATUS_LABELS = {
  pending: "Waiting to import",
  processing: "Importing",
  needs_password: "Needs the PDF's password",
  needs_review: "Imported — please check",
  completed: "Imported",
  failed: "Failed",
};

/** True while the worker still has the upload: poll it. */
export function isUnfinished(status) {
  return UNFINISHED_STATUSES.includes(status);
}

/** Only a PDF can be password-protected, so only a PDF gets the field. */
export function isPdf(file) {
  return Boolean(file) && (file.type === "application/pdf" || /\.pdf$/i.test(file.name));
}

/**
 * Why `file` can't be uploaded, as a sentence for the user, or null if it
 * can. Size only: the type is checked by the backend, which looks at the
 * file's content rather than its name.
 */
export function fileProblem(file) {
  if (!file) {
    return "Choose a statement file.";
  }
  if (file.size === 0) {
    return "This file is empty.";
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    // One decimal is enough to show why; a file size is not money, so a
    // float is fine here (hard constraint 4 is about amounts).
    const megabytes = (file.size / (1024 * 1024)).toFixed(1);
    return `This file is ${megabytes} MB. The limit is ${MAX_UPLOAD_MB} MB.`;
  }
  return null;
}

// Problems with the statement as a whole (row_number is null).
const STATEMENT_ISSUES = {
  opening_balance_mismatch:
    "The opening balance doesn't match the balance before the first transaction.",
  closing_balance_mismatch: "The closing balance doesn't match the last transaction's balance.",
};

/**
 * What a `needs_review` upload asks the user to check (SPEC §6.4):
 * `{ rows: [12, 15], statementIssues: ["…"], more: 0 }`.
 *
 * - `rows`: the row numbers IN THE FILE whose balance doesn't add up, or
 *   whose balance is missing, so the user can find them in the statement;
 * - `statementIssues`: problems with the statement as a whole;
 * - `more`: the API lists at most 100 issues; this is how many it left out.
 */
export function reviewSummary(upload) {
  const issues = upload.reconciliation_issues ?? [];
  const rows = [
    ...new Set(issues.map((issue) => issue.row_number).filter((row) => row !== null)),
  ].sort((a, b) => a - b);
  const statementIssues = issues
    .filter((issue) => issue.row_number === null)
    .map((issue) => STATEMENT_ISSUES[issue.kind] ?? "The statement's balances don't add up.");
  return {
    rows,
    statementIssues: [...new Set(statementIssues)],
    more: Math.max(0, upload.reconciliation_errors - issues.length),
  };
}

/** The list of uploads, newest first: `{ data: [upload, …], isPending, error }`. */
export function useUploads() {
  const { status, client } = useAuth();
  return useQuery({
    queryKey: UPLOADS_QUERY_KEY,
    queryFn: ({ signal }) => client.request("/uploads", { signal }),
    enabled: status === "authenticated",
  });
}

/** Swap one upload in the cached list for a newer copy of it. */
function replaceInList(queryClient, upload) {
  queryClient.setQueryData(UPLOADS_QUERY_KEY, (list) =>
    list?.map((item) => (item.id === upload.id ? upload : item)),
  );
}

// The API's own pace, `Retry-After`, is for ONE upload. GET /uploads/{id}
// has its own limit, UPLOAD_POLL_LIMIT (120/minute per user), and several
// uploads polled at that pace each would add up: 6 uploads waiting for a
// stopped worker, at 2 s each, are 180 requests a minute, and the API
// starts answering 429. So the polls SHARE the pace: with n uploads
// waiting, each one waits n times as long, and the total stays at one
// request per `Retry-After`, however many there are (review of PR 33).
/**
 * How long to wait before polling one upload again, in milliseconds, or
 * `false` to stop.
 *
 * - After a 429, the 429's own `Retry-After` comes first: the API has said
 *   how long to back off, so the regular pace must not override it.
 * - Otherwise the last answer's `Retry-After`, times the number of uploads
 *   being polled. A final answer has no header: stop.
 * - A `Retry-After: 0` still waits a second: TanStack Query reads an
 *   interval of 0 as "no interval", which would stop the polling.
 */
export function pollDelayMs({ retryAfterSeconds, error, unfinishedCount }) {
  if (error instanceof ApiError && error.status === 429) {
    return Math.max(error.retryAfterSeconds ?? 60, 1) * 1000;
  }
  if (retryAfterSeconds == null) {
    return false;
  }
  return Math.max(retryAfterSeconds, 1) * 1000 * Math.max(unfinishedCount, 1);
}

/**
 * The polling query of one upload (for useQueries below).
 *
 * Every answer is written into the cached LIST, so whatever shows the list
 * (the uploads page, the home page's latest upload) updates by itself.
 * On a final status every OTHER query is marked stale and refetched: new
 * transactions change what the other pages show. The polls are left out,
 * so a poll still running isn't cancelled. A 404 means the upload was
 * deleted (in another tab): refetch the list, and its row disappears.
 */
function pollQuery({ upload, client, queryClient, enabled, unfinishedCount }) {
  return {
    queryKey: [POLL_QUERY_ROOT, upload.id],
    queryFn: async ({ signal }) => {
      try {
        const result = await client.request(`/uploads/${upload.id}`, {
          signal,
          withRetryAfter: true,
        });
        replaceInList(queryClient, result.data);
        if (!isUnfinished(result.data.status)) {
          queryClient.invalidateQueries({
            predicate: (query) => query.queryKey[0] !== POLL_QUERY_ROOT,
          });
        }
        return result;
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          queryClient.invalidateQueries({ queryKey: UPLOADS_QUERY_KEY });
        }
        throw error;
      }
    },
    enabled,
    refetchInterval: (query) =>
      pollDelayMs({
        retryAfterSeconds: query.state.data?.retryAfterSeconds,
        error: query.state.error,
        unfinishedCount,
      }),
  };
}

/**
 * Keep every pending/processing upload fresh until its import ends.
 *
 * Mounted ONCE, by UploadsWatcher in the signed-in layout, so it keeps
 * running across pages: an upload finishing while the user is on another
 * page still updates the list and refreshes the other pages' data. It used
 * to live in each upload's row, so an upload that wasn't on screen (the
 * home page shows only the latest) was never polled and its finish
 * refreshed nothing (review of PR 33).
 *
 * It reads the list from the cache only (`enabled: false`): it never
 * fetches GET /uploads itself, it watches whatever the pages fetched.
 * `useQueries` is useQuery for a list whose length changes: one polling
 * query per unfinished upload.
 *
 * TanStack Query pauses `refetchInterval` while the browser tab is hidden
 * (refetchIntervalInBackground defaults to false), so a forgotten tab
 * doesn't poll for nothing.
 */
export function useUnfinishedUploadsPolling() {
  const { status, client } = useAuth();
  const queryClient = useQueryClient();
  const { data: uploads } = useQuery({
    queryKey: UPLOADS_QUERY_KEY,
    queryFn: ({ signal }) => client.request("/uploads", { signal }),
    enabled: false,
  });
  const unfinished = (uploads ?? []).filter((upload) => isUnfinished(upload.status));
  useQueries({
    queries: unfinished.map((upload) =>
      pollQuery({
        upload,
        client,
        queryClient,
        enabled: status === "authenticated",
        unfinishedCount: unfinished.length,
      }),
    ),
  });
}

/**
 * Upload a statement: `mutateAsync({ file, accountId, password })`.
 *
 * Sent as a multipart form (FormData), the one request in the app that
 * isn't JSON: it carries a file. `password` is added only when one was
 * typed; it goes to the API once and is never kept (CLAUDE.md §7.2).
 *
 * After a 409 that names an upload (`details.upload_id`), that upload
 * exists, so the list is refetched too: a locked PDF sent without its
 * password IS stored, as `needs_password`, and its row asks for it.
 */
export function useCreateUpload() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, accountId, password }) => {
      const form = new FormData();
      form.append("file", file);
      form.append("account_id", accountId);
      if (password) {
        form.append("password", password);
      }
      return client.request("/uploads", { method: "POST", body: form });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: UPLOADS_QUERY_KEY }),
    onError: (error) => {
      if (error instanceof ApiError && error.details?.upload_id) {
        queryClient.invalidateQueries({ queryKey: UPLOADS_QUERY_KEY });
      }
    },
  });
}

/**
 * Send a locked PDF's password: `mutateAsync({ id, password })`.
 *
 * The answer is the upload, `pending` again; putting it into the list
 * makes its row start polling. A wrong password is a 400
 * INCORRECT_PASSWORD and the upload keeps waiting, so the user can try
 * again. A 404 (deleted) or 409 (no longer locked, e.g. unlocked in
 * another tab) means the list is out of date: refetch it.
 */
export function useUnlockUpload() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, password }) =>
      client.request(`/uploads/${id}/password`, { method: "POST", body: { password } }),
    onSuccess: (upload) => replaceInList(queryClient, upload),
    onError: (error) => {
      if (error instanceof ApiError && (error.status === 404 || error.status === 409)) {
        queryClient.invalidateQueries({ queryKey: UPLOADS_QUERY_KEY });
      }
    },
  });
}

/**
 * Undo an import: `mutateAsync(id)`. Deletes the upload AND every
 * transaction it added, so all cached data is refetched (as when an
 * account is deleted). A 409 is the backend refusing, with a message that
 * says why (being imported right now; a later statement relies on this
 * one): nothing changed, so nothing is refetched.
 */
export function useDeleteUpload() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id) => client.request(`/uploads/${id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries(),
    onError: (error) => {
      if (error instanceof ApiError && error.status === 404) {
        queryClient.invalidateQueries({ queryKey: UPLOADS_QUERY_KEY });
      }
    },
  });
}
