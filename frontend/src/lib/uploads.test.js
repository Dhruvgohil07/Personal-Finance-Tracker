import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import {
  MAX_UPLOAD_BYTES,
  MAX_UPLOAD_MB,
  fileProblem,
  isPdf,
  isUnfinished,
  pollDelayMs,
  reviewSummary,
} from "@/lib/uploads";

// A File of `size` bytes, without allocating them: only `size` is read.
function fakeFile(name, size, type = "") {
  const file = new File([], name, { type });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

describe("fileProblem", () => {
  it("accepts a file within the limit", () => {
    expect(fileProblem(fakeFile("statement.xls", 200_000))).toBeNull();
  });

  it("accepts a file of exactly the limit, as the backend does", () => {
    expect(fileProblem(fakeFile("statement.pdf", MAX_UPLOAD_BYTES))).toBeNull();
  });

  it("refuses one byte over the limit, saying both sizes", () => {
    expect(fileProblem(fakeFile("big.pdf", MAX_UPLOAD_BYTES + 1))).toBe(
      `This file is 10.0 MB. The limit is ${MAX_UPLOAD_MB} MB.`,
    );
  });

  it("refuses a file the Next.js proxy would turn into a bare 500 (over 12 MB)", () => {
    expect(fileProblem(fakeFile("scan.pdf", 15 * 1024 * 1024))).toBe(
      "This file is 15.0 MB. The limit is 10 MB.",
    );
  });

  it("refuses an empty file and no file at all", () => {
    expect(fileProblem(fakeFile("empty.csv", 0))).toBe("This file is empty.");
    expect(fileProblem(null)).toBe("Choose a statement file.");
  });
});

describe("isPdf", () => {
  it.each([
    [fakeFile("statement.pdf", 1), true],
    [fakeFile("STATEMENT.PDF", 1), true],
    [fakeFile("no-extension", 1, "application/pdf"), true],
    [fakeFile("statement.xls", 1, "application/vnd.ms-excel"), false],
    [fakeFile("pdf.csv", 1, "text/csv"), false],
    [null, false],
  ])("%o → %s", (file, expected) => {
    expect(isPdf(file)).toBe(expected);
  });
});

describe("isUnfinished", () => {
  it("is true only while the worker has the upload", () => {
    expect(isUnfinished("pending")).toBe(true);
    expect(isUnfinished("processing")).toBe(true);
    // needs_password waits for the USER: polling would never change it.
    for (const status of ["needs_password", "needs_review", "completed", "failed"]) {
      expect(isUnfinished(status)).toBe(false);
    }
  });
});

describe("reviewSummary", () => {
  it("lists each file row once, in order", () => {
    const upload = {
      reconciliation_errors: 3,
      reconciliation_issues: [
        { row_number: 40, kind: "balance_mismatch" },
        { row_number: 12, kind: "missing_balance" },
        { row_number: 40, kind: "missing_balance" },
      ],
    };
    expect(reviewSummary(upload)).toEqual({ rows: [12, 40], statementIssues: [], more: 0 });
  });

  it("explains problems with the statement as a whole", () => {
    const upload = {
      reconciliation_errors: 2,
      reconciliation_issues: [
        { row_number: null, kind: "opening_balance_mismatch" },
        { row_number: null, kind: "closing_balance_mismatch" },
      ],
    };
    expect(reviewSummary(upload).statementIssues).toEqual([
      "The opening balance doesn't match the balance before the first transaction.",
      "The closing balance doesn't match the last transaction's balance.",
    ]);
  });

  it("has a general sentence for a kind it doesn't know", () => {
    const upload = {
      reconciliation_errors: 1,
      reconciliation_issues: [{ row_number: null, kind: "something_new" }],
    };
    expect(reviewSummary(upload).statementIssues).toEqual([
      "The statement's balances don't add up.",
    ]);
  });

  it("counts the issues the API left out of its first hundred", () => {
    const issues = Array.from({ length: 100 }, (_, index) => ({
      row_number: index + 2,
      kind: "balance_mismatch",
    }));
    const upload = { reconciliation_errors: 130, reconciliation_issues: issues };
    expect(reviewSummary(upload).more).toBe(30);
  });

  it("copes with no issue list (nothing could be checked)", () => {
    const upload = { reconciliation_errors: 0, reconciliation_issues: null };
    expect(reviewSummary(upload)).toEqual({ rows: [], statementIssues: [], more: 0 });
  });
});

describe("pollDelayMs", () => {
  const tooMany = (retryAfterSeconds) =>
    new ApiError({ status: 429, code: "RATE_LIMITED", message: "Too many.", retryAfterSeconds });

  it("waits as long as the API's Retry-After for a single upload", () => {
    expect(pollDelayMs({ retryAfterSeconds: 2, error: null, unfinishedCount: 1 })).toBe(2000);
  });

  it("shares the pace among several uploads, so the total stays the same", () => {
    // 6 uploads, each every 12 s: still one request every 2 s in all,
    // 30 a minute, well inside the 120/minute poll limit.
    expect(pollDelayMs({ retryAfterSeconds: 2, error: null, unfinishedCount: 6 })).toBe(12_000);
  });

  it("stops once an answer has no Retry-After (a final status)", () => {
    expect(pollDelayMs({ retryAfterSeconds: null, error: null, unfinishedCount: 1 })).toBe(false);
  });

  it("backs off for as long as a 429 says, ahead of the regular pace", () => {
    expect(pollDelayMs({ retryAfterSeconds: 2, error: tooMany(45), unfinishedCount: 1 })).toBe(
      45_000,
    );
  });

  it("waits a minute after a 429 that doesn't say how long", () => {
    expect(pollDelayMs({ retryAfterSeconds: 2, error: tooMany(null), unfinishedCount: 1 })).toBe(
      60_000,
    );
  });

  it("keeps polling on another error, at the last known pace", () => {
    const serverError = new ApiError({ status: 500, code: "INTERNAL_ERROR", message: "Oops." });
    expect(pollDelayMs({ retryAfterSeconds: 2, error: serverError, unfinishedCount: 1 })).toBe(
      2000,
    );
  });

  it("never returns 0, which TanStack Query would read as 'stop'", () => {
    expect(pollDelayMs({ retryAfterSeconds: 0, error: null, unfinishedCount: 1 })).toBe(1000);
  });
});
