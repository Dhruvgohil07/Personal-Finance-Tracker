import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { UploadsView } from "@/components/uploads/uploads-view";
import { UploadsWatcher } from "@/components/uploads/uploads-watcher";
import { MAX_UPLOAD_BYTES, UPLOADS_QUERY_KEY } from "@/lib/uploads";
import { emptyDashboardHandler } from "@/test/insights";
import { apiError, json, renderWithSession } from "@/test/session";

import HomePage from "@/app/(app)/page";

// The home page's dashboard reads the URL (?month=) and can change it;
// next/navigation only works inside a running Next.js app, so it is faked.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

const SALARY = {
  id: "11111111-1111-4111-8111-111111111111",
  bank_code: "ICICI",
  nickname: "Salary account",
  account_type: "savings",
  masked_number: "4321",
  created_at: "2026-11-01T10:00:00Z",
};

// An upload as GET /uploads returns it; `overrides` changes what a test needs.
function anUpload(overrides = {}) {
  return {
    id: "aaaaaaaa-0000-4000-8000-000000000001",
    account_id: SALARY.id,
    original_filename: "october.xls",
    file_type: "xls",
    status: "completed",
    period_start: "2026-10-01",
    period_end: "2026-10-31",
    rows_parsed: 42,
    rows_inserted: 40,
    rows_duplicate: 2,
    reconciliation_errors: 0,
    reconciliation_issues: [],
    error_message: null,
    created_at: "2026-11-03T10:00:00Z",
    completed_at: "2026-11-03T10:00:03Z",
    ...overrides,
  };
}

// What a finished import of a fresh upload looks like.
const IMPORTED = {
  status: "completed",
  rows_parsed: 31,
  rows_inserted: 31,
  rows_duplicate: 0,
  period_start: "2026-10-01",
  period_end: "2026-10-31",
};

const UNLOCK_PASSWORD = "right-password";

/**
 * A pretend uploads API, kept in memory like the real one.
 *
 * - `POST /uploads` stores a `pending` upload, unless `createError(options)`
 *   returns an error answer for this request.
 *   A PDF sent without a password becomes `needs_password` with a 409
 *   PASSWORD_REQUIRED, as the backend does.
 * - `GET /uploads/<id>` plays the upload's `script`: each poll moves it to
 *   the next state. While unfinished it sends `Retry-After: 1`, so the page
 *   polls again a second later; a final answer has no header.
 * - `POST /uploads/<id>/password` unlocks with UNLOCK_PASSWORD only.
 * - `DELETE /uploads/<id>` removes it, or answers `deleteError`.
 */
function fakeUploadsApi({
  accounts = [SALARY],
  uploads = [],
  script = [IMPORTED],
  createError,
  deleteError,
} = {}) {
  const state = {
    accounts,
    uploads: uploads.map((upload) => ({ ...upload })),
    scripts: new Map(),
    next: 2,
  };

  function answer(upload, status = 200) {
    const unfinished = ["pending", "processing"].includes(upload.status);
    return json(status, upload, unfinished ? { "Retry-After": "1" } : {});
  }

  function handler(path, options) {
    const method = options.method ?? "GET";
    if (path === "/accounts" && method === "GET") {
      return json(200, state.accounts);
    }
    if (path === "/uploads" && method === "GET") {
      return json(200, state.uploads);
    }
    if (path === "/uploads" && method === "POST") {
      const failure = createError?.(options);
      if (failure) {
        return failure;
      }
      const file = options.body.get("file");
      const locked = file.name.endsWith(".pdf") && !options.body.get("password");
      const upload = anUpload({
        id: `aaaaaaaa-0000-4000-8000-00000000000${state.next++}`,
        account_id: options.body.get("account_id"),
        original_filename: file.name,
        file_type: file.name.split(".").pop(),
        status: locked ? "needs_password" : "pending",
        period_start: null,
        period_end: null,
        rows_parsed: 0,
        rows_inserted: 0,
        rows_duplicate: 0,
        completed_at: null,
      });
      state.uploads.unshift(upload); // newest first
      state.scripts.set(upload.id, [...script]);
      if (locked) {
        return apiError(409, "PASSWORD_REQUIRED", "This PDF is password-protected.", {
          details: { upload_id: upload.id },
        });
      }
      return answer(upload, 202);
    }
    const unlock = path.match(/^\/uploads\/([^/]+)\/password$/);
    if (unlock && method === "POST") {
      const upload = state.uploads.find((u) => u.id === unlock[1]);
      if (JSON.parse(options.body).password !== UNLOCK_PASSWORD) {
        return apiError(400, "INCORRECT_PASSWORD", "That password does not open this PDF.");
      }
      upload.status = "pending";
      return answer(upload, 202);
    }
    const one = path.match(/^\/uploads\/([^/]+)$/);
    if (one && method === "GET") {
      const upload = state.uploads.find((u) => u.id === one[1]);
      const nextState = state.scripts.get(upload.id)?.shift();
      Object.assign(upload, nextState ?? {});
      return answer(upload);
    }
    if (one && method === "DELETE") {
      if (deleteError) {
        return deleteError();
      }
      state.uploads = state.uploads.filter((u) => u.id !== one[1]);
      return new Response(null, { status: 204 });
    }
    // The home page's dashboard: an empty month.
    return emptyDashboardHandler(path);
  }
  return { state, handler };
}

const requestsTo = (fetchImpl, method, path) =>
  fetchImpl.mock.calls.filter(
    ([url, options]) => url === `/api/v1${path}` && (options?.method ?? "GET") === method,
  );

const polls = (fetchImpl) =>
  fetchImpl.mock.calls.filter(
    ([url, options]) => /\/uploads\/[^/]+$/.test(url) && (options?.method ?? "GET") === "GET",
  );

function fakeFile(name, { size, type = "" } = {}) {
  const file = new File(["statement"], name, { type });
  if (size !== undefined) {
    // Only `size` is read, so a "15 MB" file needs no 15 MB of memory.
    Object.defineProperty(file, "size", { value: size });
  }
  return file;
}

function chooseFile(file) {
  fireEvent.change(screen.getByLabelText("Statement file"), { target: { files: [file] } });
}

const uploadButton = () => screen.getByRole("button", { name: "Upload statement" });

// A page as the signed-in layout renders it: with the UploadsWatcher next
// to it, which does the polling.
function withWatcher(page) {
  return (
    <>
      <UploadsWatcher />
      {page}
    </>
  );
}

async function renderUploads(api) {
  const rendered = renderWithSession(withWatcher(<UploadsView />), {
    signedIn: true,
    handler: api.handler,
  });
  await screen.findByRole("heading", { name: "Upload a statement" });
  return rendered;
}

describe("UploadsView: before there is anything to upload into", () => {
  it("asks for an account first", async () => {
    renderWithSession(<UploadsView />, {
      signedIn: true,
      handler: fakeUploadsApi({ accounts: [] }).handler,
    });

    expect(await screen.findByText(/add an account/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "add an account" })).toHaveAttribute(
      "href",
      "/accounts",
    );
    expect(screen.queryByLabelText("Statement file")).not.toBeInTheDocument();
  });
});

describe("UploadsView: the history", () => {
  it("shows what each import did, in words", async () => {
    const api = fakeUploadsApi({
      uploads: [
        anUpload(),
        anUpload({
          id: "aaaaaaaa-0000-4000-8000-0000000000a2",
          original_filename: "september.pdf",
          status: "needs_review",
          rows_duplicate: 0,
          reconciliation_errors: 3,
          reconciliation_issues: [
            { row_number: 15, kind: "balance_mismatch" },
            { row_number: 12, kind: "missing_balance" },
            { row_number: null, kind: "closing_balance_mismatch" },
          ],
        }),
        anUpload({
          id: "aaaaaaaa-0000-4000-8000-0000000000a3",
          original_filename: "notes.csv",
          status: "failed",
          error_message: "We could not recognise this statement's layout.",
        }),
      ],
    });
    await renderUploads(api);

    const completed = (await screen.findByText("october.xls")).closest("li");
    expect(completed).toHaveTextContent("Salary account · uploaded 3 Nov 2026");
    expect(completed).toHaveTextContent("Imported");
    expect(completed).toHaveTextContent(
      "40 new transactions added, 2 transactions already imported before. Statement period: 1 Oct 2026 – 31 Oct 2026.",
    );

    const review = screen.getByText("september.pdf").closest("li");
    expect(review).toHaveTextContent("Imported — please check");
    expect(review).toHaveTextContent("Rows 12, 15 of the file");
    expect(review).toHaveTextContent(
      "The closing balance doesn't match the last transaction's balance.",
    );

    const failed = screen.getByText("notes.csv").closest("li");
    expect(failed).toHaveTextContent("Failed");
    expect(failed).toHaveTextContent("We could not recognise this statement's layout.");
  });

  it("says so when there are no uploads yet", async () => {
    await renderUploads(fakeUploadsApi());

    expect(await screen.findByText("No uploads yet.")).toBeInTheDocument();
  });

  it("does not poll uploads that are already final or wait for a password", async () => {
    const api = fakeUploadsApi({
      uploads: [
        anUpload(),
        anUpload({
          id: "aaaaaaaa-0000-4000-8000-0000000000a2",
          original_filename: "locked.pdf",
          status: "needs_password",
        }),
      ],
    });
    const { fetchImpl } = await renderUploads(api);

    await screen.findByText("locked.pdf");
    expect(polls(fetchImpl)).toHaveLength(0);
  });
});

describe("UploadsView: uploading", () => {
  it(
    "sends the file to the chosen account, then polls until the import is done",
    { timeout: 10_000 },
    async () => {
      const api = fakeUploadsApi({ script: [{ status: "processing" }, IMPORTED] });
      const { fetchImpl } = await renderUploads(api);

      chooseFile(fakeFile("october.xls"));
      // Only a PDF can be locked, so only a PDF gets a password field.
      expect(screen.queryByLabelText(/PDF password/)).not.toBeInTheDocument();
      fireEvent.click(uploadButton());

      expect(await screen.findByText(/october\.xls was uploaded/)).toBeInTheDocument();
      const [[, options]] = requestsTo(fetchImpl, "POST", "/uploads");
      expect(options.body.get("file").name).toBe("october.xls");
      expect(options.body.get("account_id")).toBe(SALARY.id);
      expect(options.body.has("password")).toBe(false);

      // pending, then processing: the page keeps saying so...
      const row = (await screen.findByText("october.xls", { selector: "p" })).closest("li");
      expect(within(row).getByRole("status")).toHaveTextContent("Importing…");
      // ...and asks again after the API's Retry-After (1 s here), until done.
      await waitFor(
        () => expect(row).toHaveTextContent("31 new transactions added."),
        { timeout: 3000 },
      );
      expect(polls(fetchImpl)).toHaveLength(2);
      // "…is being imported" is no longer true, so it is no longer said.
      expect(screen.queryByText(/october\.xls was uploaded/)).not.toBeInTheDocument();

      // A final answer has no Retry-After: no more polling.
      await new Promise((resolve) => setTimeout(resolve, 1500));
      expect(polls(fetchImpl)).toHaveLength(2);
    },
  );

  it("refuses a file over the size limit without sending it", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());

    chooseFile(fakeFile("scan.pdf", { size: MAX_UPLOAD_BYTES + 1 }));

    // Said at once, before the button is even pressed.
    expect(screen.getByText("This file is 10.0 MB. The limit is 10 MB.")).toBeInTheDocument();
    expect(screen.getByLabelText("Statement file")).toHaveAttribute("aria-invalid", "true");

    fireEvent.click(uploadButton());
    expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(0);
  });

  it("asks for a file when none was chosen", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());

    fireEvent.click(uploadButton());

    expect(screen.getByText("Choose a statement file.")).toBeInTheDocument();
    expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(0);
  });

  it("sends a PDF's password with it when one is typed", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());

    chooseFile(fakeFile("october.pdf", { type: "application/pdf" }));
    fireEvent.change(screen.getByLabelText("PDF password (if it has one)"), {
      target: { value: "secret" },
    });
    fireEvent.click(uploadButton());

    // The form forgets the password (and the file) once it is sent.
    await waitFor(() => expect(screen.queryByLabelText(/PDF password/)).not.toBeInTheDocument());
    const [[, options]] = requestsTo(fetchImpl, "POST", "/uploads");
    expect(options.body.get("password")).toBe("secret");
  });

  it("keeps the file and asks again when the PDF's password is wrong", async () => {
    // A wrong password on the upload itself is a 400 and stores nothing.
    const api = fakeUploadsApi({
      createError: (options) =>
        options.body.get("password") === "wrong"
          ? apiError(400, "INCORRECT_PASSWORD", "That password does not open this PDF.")
          : undefined,
    });
    const { fetchImpl } = await renderUploads(api);

    chooseFile(fakeFile("october.pdf", { type: "application/pdf" }));
    fireEvent.change(screen.getByLabelText("PDF password (if it has one)"), {
      target: { value: "wrong" },
    });
    fireEvent.click(uploadButton());

    expect(await screen.findByText("That password does not open this PDF.")).toBeInTheDocument();
    // Emptied, so nothing of the wrong password stays on the page.
    expect(screen.getByLabelText("PDF password (if it has one)")).toHaveValue("");

    fireEvent.change(screen.getByLabelText("PDF password (if it has one)"), {
      target: { value: "right" },
    });
    fireEvent.click(uploadButton());

    // The same file went again, without choosing it a second time.
    await waitFor(() => expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(2));
    const [, [, second]] = requestsTo(fetchImpl, "POST", "/uploads");
    expect(second.body.get("file").name).toBe("october.pdf");
    expect(second.body.get("password")).toBe("right");
  });

  it("explains a file that was uploaded before", async () => {
    const api = fakeUploadsApi({
      createError: () =>
        apiError(409, "CONFLICT", "You have already uploaded this file.", {
          details: { upload_id: "aaaaaaaa-0000-4000-8000-000000000001" },
        }),
    });
    await renderUploads(api);

    chooseFile(fakeFile("october.xls"));
    fireEvent.click(uploadButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You have already uploaded this file.",
    );
  });

  it("says how long to wait after the hourly upload limit", async () => {
    const api = fakeUploadsApi({
      createError: () =>
        apiError(429, "RATE_LIMITED", "Too many requests.", {
          headers: { "Retry-After": "1800" },
        }),
    });
    await renderUploads(api);

    chooseFile(fakeFile("october.xls"));
    fireEvent.click(uploadButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many attempts. Try again in 30 minutes.",
    );
  });
});

describe("UploadsView: a password-protected PDF", () => {
  it(
    "asks for the password, lets a wrong one be retried, then imports",
    { timeout: 10_000 },
    async () => {
      const { fetchImpl } = await renderUploads(fakeUploadsApi());

      // Sent without a password: the backend keeps it and answers 409.
      chooseFile(fakeFile("locked.pdf", { type: "application/pdf" }));
      fireEvent.click(uploadButton());

      expect(
        await screen.findByText(/locked\.pdf is password-protected\. Enter its password/),
      ).toBeInTheDocument();
      const row = (await screen.findByText("locked.pdf", { selector: "p" })).closest("li");
      expect(row).toHaveTextContent("Needs the PDF's password");

      const field = within(row).getByLabelText("Password for locked.pdf");
      fireEvent.change(field, { target: { value: "wrong" } });
      fireEvent.click(within(row).getByRole("button", { name: "Unlock and import" }));

      expect(
        await within(row).findByText("That password does not open this PDF."),
      ).toBeInTheDocument();
      expect(field).toHaveValue("");

      fireEvent.change(field, { target: { value: UNLOCK_PASSWORD } });
      fireEvent.click(within(row).getByRole("button", { name: "Unlock and import" }));

      await waitFor(() => expect(row).toHaveTextContent("31 new transactions added."), {
        timeout: 3000,
      });
      // Unlocked: "is password-protected" is no longer said above the form.
      expect(screen.queryByText(/locked\.pdf is password-protected/)).not.toBeInTheDocument();
      // The password went only in the body of the unlock request, alone.
      const unlocks = fetchImpl.mock.calls.filter(([url]) => url.endsWith("/password"));
      expect(unlocks).toHaveLength(2);
      expect(JSON.parse(unlocks[1][1].body)).toEqual({ password: UNLOCK_PASSWORD });
      expect(unlocks[1][0]).not.toContain(UNLOCK_PASSWORD);
    },
  );
});

describe("UploadsView: deleting", () => {
  it("asks first, saying how many transactions go with it, then deletes", async () => {
    const api = fakeUploadsApi({ uploads: [anUpload()] });
    await renderUploads(api);

    fireEvent.click(await screen.findByRole("button", { name: "Delete october.xls" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete october.xls?" });
    expect(dialog).toHaveAccessibleDescription(
      "Delete october.xls? This also deletes the 40 transactions it added. It can't be undone.",
    );
    // A stray Enter lands on Cancel, not on Delete.
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete upload" }));

    expect(await screen.findByText("No uploads yet.")).toBeInTheDocument();
  });

  it("shows why the backend refused, and keeps the upload", async () => {
    const message =
      "A later statement for this account overlaps this one and relies on its transactions. Delete that later upload first, then this one.";
    const api = fakeUploadsApi({
      uploads: [anUpload()],
      deleteError: () => apiError(409, "CONFLICT", message),
    });
    await renderUploads(api);

    fireEvent.click(await screen.findByRole("button", { name: "Delete october.xls" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete upload" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByText("october.xls", { selector: "p" })).toBeInTheDocument();
  });

  it("says nothing is lost when deleting an upload that imported nothing", async () => {
    const api = fakeUploadsApi({
      uploads: [anUpload({ status: "failed", rows_inserted: 0, error_message: "Unreadable." })],
    });
    await renderUploads(api);

    fireEvent.click(await screen.findByRole("button", { name: "Delete october.xls" }));

    expect(screen.getByRole("alertdialog")).toHaveTextContent(
      "Nothing from it has been imported, so no transactions are deleted.",
    );
  });
});

describe("HomePage: the first statement", () => {
  it("asks for the first statement, then shows what it imported", async () => {
    renderWithSession(withWatcher(<HomePage />), {
      signedIn: true,
      handler: fakeUploadsApi().handler,
    });

    expect(await screen.findByText("Upload your first statement")).toBeInTheDocument();

    chooseFile(fakeFile("october.xls"));
    fireEvent.click(screen.getByRole("button", { name: "Upload my first statement" }));

    expect(await screen.findByText("Your latest statement")).toBeInTheDocument();
    // Watched importing on this page, so its result stays once it is done,
    // with the dashboard below it.
    expect(await screen.findByText(/31 new transactions added\./)).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Your month" })).toBeInTheDocument();
    expect(screen.getByText("october.xls")).toBeInTheDocument();
  });

  it("shows only the latest upload once there are several", async () => {
    renderWithSession(withWatcher(<HomePage />), {
      signedIn: true,
      handler: fakeUploadsApi({
        uploads: [
          anUpload({ original_filename: "november.xls", status: "needs_password" }),
          anUpload({ id: "aaaaaaaa-0000-4000-8000-0000000000a2", original_filename: "october.xls" }),
        ],
      }).handler,
    });

    expect(await screen.findByText("november.xls")).toBeInTheDocument();
    expect(screen.queryByText("october.xls")).not.toBeInTheDocument();
  });

  it("leaves out a latest upload that imported cleanly before the page opened", async () => {
    renderWithSession(withWatcher(<HomePage />), {
      signedIn: true,
      handler: fakeUploadsApi({ uploads: [anUpload({ original_filename: "october.xls" })] }).handler,
    });

    expect(await screen.findByRole("region", { name: "Your month" })).toBeInTheDocument();
    expect(screen.queryByText("Your latest statement")).not.toBeInTheDocument();
    expect(screen.queryByText("october.xls")).not.toBeInTheDocument();
  });

  it.each([
    ["needs_review", { reconciliation_errors: 1, reconciliation_issues: [{ row_number: 7, kind: "balance_mismatch" }] }],
    ["failed", { error_message: "This file could not be read." }],
  ])("keeps a latest upload that is %s above the dashboard", async (status, extra) => {
    renderWithSession(withWatcher(<HomePage />), {
      signedIn: true,
      handler: fakeUploadsApi({
        uploads: [anUpload({ original_filename: "october.xls", status, ...extra })],
      }).handler,
    });

    expect(await screen.findByText("Your latest statement")).toBeInTheDocument();
    expect(screen.getByText("october.xls")).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Your month" })).toBeInTheDocument();
  });
});

// Review of PR 33.
const RENT = { ...SALARY, id: "22222222-2222-4222-8222-222222222222", nickname: "Rent" };

describe("UploadsView: review fixes", () => {
  it("uploads into the account the drop-down shows after the chosen one is deleted", async () => {
    const api = fakeUploadsApi({ accounts: [SALARY, RENT] });
    const { fetchImpl, queryClient } = await renderUploads(api);
    expect(screen.getByLabelText("Account")).toHaveDisplayValue("Salary account (•••• 4321)");

    // Salary is deleted elsewhere (another tab); this tab refetches.
    api.state.accounts = [RENT];
    await act(() => queryClient.invalidateQueries({ queryKey: ["accounts"] }));

    await waitFor(() =>
      expect(screen.getByLabelText("Account")).toHaveDisplayValue("Rent (•••• 4321)"),
    );
    chooseFile(fakeFile("october.xls"));
    fireEvent.click(uploadButton());

    await waitFor(() => expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(1));
    const [[, options]] = requestsTo(fetchImpl, "POST", "/uploads");
    expect(options.body.get("account_id")).toBe(RENT.id);
  });

  it("keeps browsers' password managers out of both statement password fields", async () => {
    const api = fakeUploadsApi({
      uploads: [anUpload({ original_filename: "locked.pdf", status: "needs_password" })],
    });
    await renderUploads(api);

    chooseFile(fakeFile("october.pdf", { type: "application/pdf" }));

    // "off" is ignored on password fields; see STATEMENT_PASSWORD_AUTOCOMPLETE.
    expect(screen.getByLabelText("PDF password (if it has one)")).toHaveAttribute(
      "autocomplete",
      "one-time-code",
    );
    expect(await screen.findByLabelText("Password for locked.pdf")).toHaveAttribute(
      "autocomplete",
      "one-time-code",
    );
  });
});

describe("UploadsWatcher", () => {
  it("polls an upload that isn't on screen until its import is done", async () => {
    const older = anUpload({
      id: "aaaaaaaa-0000-4000-8000-0000000000a2",
      original_filename: "september.xls",
      status: "pending",
      rows_parsed: 0,
      rows_inserted: 0,
      rows_duplicate: 0,
    });
    // october.xls failed, so the home page keeps it in view.
    const api = fakeUploadsApi({
      uploads: [
        anUpload({ original_filename: "october.xls", status: "failed", error_message: "Unreadable." }),
        older,
      ],
    });
    api.state.scripts.set(older.id, [IMPORTED]);
    // The home page shows only the latest upload, october.xls...
    const { fetchImpl, queryClient } = renderWithSession(withWatcher(<HomePage />), {
      signedIn: true,
      handler: api.handler,
    });
    expect(await screen.findByText("october.xls")).toBeInTheDocument();
    expect(screen.queryByText("september.xls")).not.toBeInTheDocument();

    // ...but september.xls is still polled, and its result reaches the list.
    await waitFor(() =>
      expect(
        queryClient.getQueryData(UPLOADS_QUERY_KEY).find((u) => u.id === older.id).status,
      ).toBe("completed"),
    );
    expect(requestsTo(fetchImpl, "GET", `/uploads/${older.id}`).length).toBeGreaterThan(0);
  });
});

describe("HomePage: review fixes", () => {
  it("shows the first-account step even when GET /uploads fails", async () => {
    const api = fakeUploadsApi({ accounts: [] });
    renderWithSession(withWatcher(<HomePage />), {
      signedIn: true,
      handler: (path, options) =>
        path === "/uploads"
          ? apiError(500, "INTERNAL_ERROR", "Something went wrong.")
          : api.handler(path, options),
    });

    expect(await screen.findByText("Add your first account")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

// Phase 3 Step 7: a CSV is uploaded with a column mapping.
const csvFile = (text, name = "statement.csv") => new File([text], name, { type: "text/csv" });

const TWO_COLUMN_CSV = [
  "Synthetic Bank - account statement",
  "Date,Narration,Withdrawal,Deposit,Balance",
  "05/08/2026,UPI SHOP,70.00,,930.00",
  "25/08/2026,SALARY,,500.00,1430.00",
  "",
].join("\n");

describe("UploadsView: a CSV and its columns", () => {
  it("pre-fills the mapping from the file's column names and sends it", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());

    chooseFile(csvFile(TWO_COLUMN_CSV));

    // Read in the browser: the title row above the table is skipped.
    expect(await screen.findByLabelText("Date column")).toHaveDisplayValue("Date");
    expect(screen.getByLabelText("Description column")).toHaveDisplayValue("Narration");
    expect(screen.getByLabelText("Money out column")).toHaveDisplayValue("Withdrawal");
    expect(screen.getByLabelText("Money in column")).toHaveDisplayValue("Deposit");
    expect(screen.getByLabelText("Balance column (optional)")).toHaveDisplayValue("Balance");
    // 25/08 can only be day-first, so the format is chosen too...
    expect(screen.getByLabelText("Date format")).toHaveDisplayValue("01/08/2026");
    // ...and the preview shows the dates as they will be read.
    expect(screen.getByText("5 Aug 2026")).toBeInTheDocument();
    expect(screen.getByText("25 Aug 2026")).toBeInTheDocument();

    fireEvent.click(uploadButton());

    await waitFor(() => expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(1));
    const [[, options]] = requestsTo(fetchImpl, "POST", "/uploads");
    expect(JSON.parse(options.body.get("column_mapping"))).toEqual({
      date_column: "Date",
      description_column: "Narration",
      date_format: "%d/%m/%Y",
      debit_column: "Withdrawal",
      credit_column: "Deposit",
      balance_column: "Balance",
    });
  });

  it("makes the user choose when a date could be day-first or month-first", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());
    chooseFile(csvFile("Date,Description,Amount\n01/08/2026,SHOP,-70.00\n"));

    const format = await screen.findByLabelText("Date format");
    expect(format).toHaveDisplayValue("Choose a format");
    fireEvent.click(uploadButton());

    expect(await screen.findByText("Choose how the dates are written.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Check the column choices below.");
    expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(0);

    // Month first: the preview says 8 January, so the mistake is visible.
    fireEvent.change(format, { target: { value: "%m/%d/%Y" } });
    expect(screen.getByText("8 Jan 2026")).toBeInTheDocument();
  });

  it("sends one signed amount column when the file has one", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());
    chooseFile(csvFile("Date,Description,Amount\n25/08/2026,SHOP,-70.00\n"));

    expect(await screen.findByLabelText("How are amounts written?")).toHaveDisplayValue(
      "One column: a minus sign means money out",
    );
    expect(screen.getByLabelText("Amount column")).toHaveDisplayValue("Amount");
    fireEvent.click(uploadButton());

    await waitFor(() => expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(1));
    const [[, options]] = requestsTo(fetchImpl, "POST", "/uploads");
    expect(JSON.parse(options.body.get("column_mapping"))).toEqual({
      date_column: "Date",
      description_column: "Description",
      date_format: "%d/%m/%Y",
      amount_column: "Amount",
    });
  });

  it("refuses a column chosen twice before sending anything", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());
    chooseFile(csvFile(TWO_COLUMN_CSV));

    fireEvent.change(await screen.findByLabelText("Money in column"), {
      target: { value: "Withdrawal" },
    });
    fireEvent.click(uploadButton());

    expect(
      screen.getByText("This column is already used above. Each column can be used once."),
    ).toBeInTheDocument();
    expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(0);
  });

  it("shows the backend's complaint about a mapping field under that field", async () => {
    const api = fakeUploadsApi({
      createError: () =>
        apiError(422, "VALIDATION_ERROR", "The request contains invalid data.", {
          details: [
            {
              field: "body.column_mapping.date_format",
              message: "Value error, is not a valid date format (e.g. %d/%m/%Y)",
            },
          ],
        }),
    });
    await renderUploads(api);
    chooseFile(csvFile(TWO_COLUMN_CSV));
    await screen.findByLabelText("Date column");

    fireEvent.click(uploadButton());

    const message = await screen.findByText(/is not a valid date format/);
    expect(screen.getByLabelText("Date format")).toHaveAccessibleDescription(
      expect.stringContaining("is not a valid date format"),
    );
    expect(message).toBeInTheDocument();
  });

  it("explains a CSV whose column names can't be found, and sends nothing", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());

    chooseFile(csvFile("12345\n67890\n"));

    expect(await screen.findByText(/couldn't find the column names/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Date column")).not.toBeInTheDocument();
    fireEvent.click(uploadButton());
    expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(0);
  });

  it("drops the mapping when another kind of file is chosen", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());
    chooseFile(csvFile(TWO_COLUMN_CSV));
    await screen.findByLabelText("Date column");

    chooseFile(fakeFile("october.xls"));
    expect(screen.queryByLabelText("Date column")).not.toBeInTheDocument();
    fireEvent.click(uploadButton());

    await waitFor(() => expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(1));
    const [[, options]] = requestsTo(fetchImpl, "POST", "/uploads");
    expect(options.body.has("column_mapping")).toBe(false);
  });
});

describe("UploadsView: CSV review fixes (PR 34)", () => {
  it("maps a semicolon file with a title line, and sends the mapping", async () => {
    // The backend now reads this with ";" too (_sniff_delimiter).
    const { fetchImpl } = await renderUploads(fakeUploadsApi());
    chooseFile(
      csvFile("Synthetic Bank statement\nDate;Narration;Amount\n25/08/2026;SHOP;-70,00\n"),
    );

    expect(await screen.findByLabelText("Date column")).toHaveDisplayValue("Date");
    expect(screen.getByLabelText("Amount column")).toHaveDisplayValue("Amount");
    fireEvent.click(uploadButton());

    await waitFor(() => expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(1));
    const [[, options]] = requestsTo(fetchImpl, "POST", "/uploads");
    expect(JSON.parse(options.body.get("column_mapping")).date_column).toBe("Date");
  });

  it("offers and sends a date format with the time when the dates carry one", async () => {
    const { fetchImpl } = await renderUploads(fakeUploadsApi());
    chooseFile(
      csvFile(
        "Date,Narration,Amount\n01/08/2026 14:05:09,SHOP,-70.00\n25/08/2026 09:00:00,PAY,500.00\n",
      ),
    );

    expect(await screen.findByLabelText("Date format")).toHaveDisplayValue("01/08/2026 14:05:09");
    expect(screen.getByText("25 Aug 2026")).toBeInTheDocument();
    fireEvent.click(uploadButton());

    await waitFor(() => expect(requestsTo(fetchImpl, "POST", "/uploads")).toHaveLength(1));
    const [[, options]] = requestsTo(fetchImpl, "POST", "/uploads");
    expect(JSON.parse(options.body.get("column_mapping")).date_format).toBe("%d/%m/%Y %H:%M:%S");
  });
});
