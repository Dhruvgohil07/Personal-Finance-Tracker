import { describe, expect, it } from "vitest";

import {
  DATE_FORMATS,
  columnLabels,
  dataRows,
  dateFormatsFor,
  dateSamples,
  formatLabel,
  formatsFitting,
  guessMapping,
  mappingProblems,
  parseDateWith,
  previewDate,
  suggestDateFormat,
  toColumnMapping,
} from "@/lib/column-mapping";

// A complete, valid two-column mapping; tests change what they need.
const VALID = {
  headerIndex: 0,
  amountStyle: "two",
  date_column: "Date",
  description_column: "Narration",
  debit_column: "Withdrawal",
  credit_column: "Deposit",
  amount_column: "",
  balance_column: "Balance",
  date_format: "%d/%m/%Y",
};

describe("columnLabels", () => {
  it("trims, drops empty cells, and keeps the first of a repeated name", () => {
    // The backend matches names ignoring case and uses the FIRST column.
    expect(columnLabels([" Date ", "", "Amount", "AMOUNT", "Balance"])).toEqual([
      "Date",
      "Amount",
      "Balance",
    ]);
  });
});

describe("guessMapping", () => {
  it("recognises a withdrawal/deposit statement", () => {
    const labels = ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"];
    expect(guessMapping(labels, 3)).toEqual({
      headerIndex: 3,
      amountStyle: "two",
      date_column: "Date",
      description_column: "Narration",
      debit_column: "Withdrawal Amt.",
      credit_column: "Deposit Amt.",
      amount_column: "",
      balance_column: "Closing Balance",
      date_format: "",
    });
  });

  it("prefers the transaction date over the value date, wherever it is", () => {
    const labels = ["Value Date", "Transaction Date", "Transaction Remarks", "Debit", "Credit"];
    const guess = guessMapping(labels, 0);
    expect(guess.date_column).toBe("Transaction Date");
    expect(guess.description_column).toBe("Transaction Remarks");
  });

  it("recognises one signed amount column", () => {
    const guess = guessMapping(["Date", "Description", "Amount (INR)", "Balance"], 0);
    expect(guess.amountStyle).toBe("one");
    expect(guess.amount_column).toBe("Amount (INR)");
    expect(guess.debit_column).toBe("");
  });

  it("leaves a choice empty when nothing fits, rather than guessing wildly", () => {
    const guess = guessMapping(["When", "What", "How much"], 0);
    expect(guess.date_column).toBe("");
    expect(guess.description_column).toBe("");
    expect(guess.amount_column).toBe("");
  });
});

describe("parseDateWith (Python strptime's rules)", () => {
  it.each([
    ["01/08/2026", "%d/%m/%Y", "2026-08-01"],
    ["1/8/2026", "%d/%m/%Y", "2026-08-01"], // one digit is fine, as in strptime
    ["01-AUG-2026", "%d-%b-%Y", "2026-08-01"], // month names ignore case
    ["01 Aug   2026", "%d %b %Y", "2026-08-01"], // a space matches several
    ["  01/08/2026 ", "%d/%m/%Y", "2026-08-01"], // the cell is trimmed first
    ["01/08/26", "%d/%m/%y", "2026-08-01"],
    ["01/08/69", "%d/%m/%y", "1969-08-01"], // 69-99 are the 1900s
    ["01/08/68", "%d/%m/%y", "2068-08-01"], // 00-68 the 2000s
    ["01 August 2026", "%d %B %Y", "2026-08-01"],
    ["2026-08-01", "%Y-%m-%d", "2026-08-01"],
    ["29/02/2024", "%d/%m/%Y", "2024-02-29"],
  ])("%s with %s is %s", (text, pattern, expected) => {
    expect(parseDateWith(text, pattern)).toBe(expected);
  });

  it.each([
    ["31/06/2026", "%d/%m/%Y"], // June has 30 days
    ["29/02/2026", "%d/%m/%Y"], // not a leap year
    ["01/13/2026", "%d/%m/%Y"], // no 13th month
    ["01/08/2026 10:30", "%d/%m/%Y"], // the WHOLE cell must match
    ["01-08-2026", "%d/%m/%Y"], // the separator is part of the format
    ["01/08/2026", "%d/%m/%y"], // %y is exactly two digits
    ["01.08.2026", "%d/%m/%Y"], // "." is a literal dot, not "any character"
  ])("refuses %s with %s", (text, pattern) => {
    expect(parseDateWith(text, pattern)).toBeNull();
  });

  it("can read every offered format's own example, with or without a time", () => {
    for (const { pattern, example } of DATE_FORMATS) {
      expect(parseDateWith(example, pattern)).toBe("2026-08-01");
    }
    // Every date-and-time combination too (all 48, from a cell no
    // combination reads, which lists them all).
    for (const { pattern, example } of dateFormatsFor(["99:99"])) {
      expect(parseDateWith(example, pattern)).toBe("2026-08-01");
    }
  });

  // Review of PR 34: a time in the date column. Every case checked
  // against Python's datetime.strptime, like those above.
  it.each([
    ["01/08/2026 14:05:09", "%d/%m/%Y %H:%M:%S", "2026-08-01"],
    ["01/08/2026 2:5", "%d/%m/%Y %H:%M", "2026-08-01"],
    ["01/08/2026 02:05 pm", "%d/%m/%Y %I:%M %p", "2026-08-01"],
    ["Aug 01, 2026", "%b %d, %Y", "2026-08-01"],
  ])("%s with %s is %s", (text, pattern, expected) => {
    expect(parseDateWith(text, pattern)).toBe(expected);
  });

  it.each([
    ["01/08/2026 10:32:60", "%d/%m/%Y %H:%M:%S"], // Python: second must be in 0..59
    ["01/08/2026 24:00", "%d/%m/%Y %H:%M"], // no hour 24
    ["01/08/2026 13:05 PM", "%d/%m/%Y %I:%M %p"], // %I is 1-12
    ["01/08/2026 14:05", "%d/%m/%Y"], // the time must be in the pattern
  ])("refuses %s with %s", (text, pattern) => {
    expect(parseDateWith(text, pattern)).toBeNull();
  });
});

describe("dateFormatsFor", () => {
  it("offers the date formats for dates without a time", () => {
    expect(dateFormatsFor(["01/08/2026"])).toBe(DATE_FORMATS);
  });

  it("offers only the date-and-time formats that fit, when there is a time", () => {
    const offered = dateFormatsFor(["01/08/2026 14:05:09", "25/08/2026 09:00:00"]);
    expect(offered.map((format) => format.pattern)).toEqual(["%d/%m/%Y %H:%M:%S"]);
    expect(formatLabel(offered[0])).toBe("01/08/2026 14:05:09");
  });

  it("keeps the note of a format, as in '(month first, US style)'", () => {
    const usStyle = DATE_FORMATS.find((format) => format.pattern === "%m/%d/%Y");
    expect(formatLabel(usStyle)).toBe("08/01/2026 (month first, US style)");
  });
});

describe("formatsFitting / suggestDateFormat", () => {
  const rowsWith = (...dates) => [["Date", "Narration"], ...dates.map((date) => [date, "SHOP"])];

  it("finds both day-first and month-first for 01/08/2026", () => {
    expect(formatsFitting(["01/08/2026"])).toEqual(["%d/%m/%Y", "%m/%d/%Y"]);
  });

  it("finds only day-first once a day above 12 appears", () => {
    expect(formatsFitting(["01/08/2026", "25/08/2026"])).toEqual(["%d/%m/%Y"]);
  });

  it("suggests a time-aware format for dates that carry a time", () => {
    const values = { ...VALID, date_format: "" };
    const rows = rowsWith("05/08/2026 10:00:00", "25/08/2026 18:30:00");
    expect(suggestDateFormat(values, rows).date_format).toBe("%d/%m/%Y %H:%M:%S");
  });

  it("decides on every row read, not just the first few", () => {
    // Review of PR 34: seven ambiguous days, then the 25th settles it.
    const values = { ...VALID, date_format: "" };
    const rows = rowsWith(
      "01/08/2026",
      "02/08/2026",
      "03/08/2026",
      "04/08/2026",
      "05/08/2026",
      "06/08/2026",
      "07/08/2026",
      "25/08/2026",
    );
    expect(suggestDateFormat(values, rows).date_format).toBe("%d/%m/%Y");
  });

  it("suggests a format only when exactly one fits", () => {
    const values = { ...VALID, date_format: "" };
    expect(suggestDateFormat(values, rowsWith("05/08/2026", "25/08/2026")).date_format).toBe(
      "%d/%m/%Y",
    );
    // 1 August or 8 January? The user decides.
    expect(suggestDateFormat(values, rowsWith("01/08/2026")).date_format).toBe("");
  });

  it("never replaces a format the user already chose", () => {
    const values = { ...VALID, date_format: "%m/%d/%Y" };
    expect(suggestDateFormat(values, rowsWith("05/08/2026", "25/08/2026")).date_format).toBe(
      "%m/%d/%Y",
    );
  });

  it("shows a date as read, or nothing when the format doesn't fit", () => {
    expect(previewDate("01/08/2026", "%d/%m/%Y")).toBe("1 Aug 2026");
    expect(previewDate("01/08/2026", "%m/%d/%Y")).toBe("8 Jan 2026");
    expect(previewDate("2026-08-01", "%d/%m/%Y")).toBeNull();
  });
});

describe("dataRows / dateSamples", () => {
  const rows = [
    ["My Bank statement"],
    ["Date", "Narration", "Amount", "amount"],
    ["01/08/2026", "UPI SHOP", "-70.00", "ignored"],
    ["", "…continued narration", "", ""],
    ["", "", "", ""],
    ["02/08/2026", "SALARY", "500.00", "ignored"],
    ["", "Total", "430.00", ""],
  ];

  it("skips rows without a date, as the backend does", () => {
    expect(dateSamples(rows, { headerIndex: 1, date_column: "Date" })).toEqual([
      "01/08/2026",
      "02/08/2026",
    ]);
  });

  it("reads a repeated column name from its FIRST column", () => {
    const [first] = dataRows(rows, 1, "Date");
    expect(first("Amount")).toBe("-70.00");
    expect(first("")).toBe("");
  });
});

describe("mappingProblems", () => {
  it("is empty for a complete mapping", () => {
    expect(mappingProblems(VALID, ["01/08/2026"])).toEqual({});
  });

  it("asks for every missing choice", () => {
    const empty = { ...VALID, date_column: "", description_column: "", debit_column: "", credit_column: "", date_format: "" };
    expect(Object.keys(mappingProblems(empty))).toEqual([
      "date_column",
      "description_column",
      "debit_column",
      "credit_column",
      "date_format",
    ]);
  });

  it("needs the one amount column in the one-column style, not the pair", () => {
    const oneColumn = { ...VALID, amountStyle: "one", amount_column: "" };
    expect(Object.keys(mappingProblems(oneColumn, ["01/08/2026"]))).toEqual(["amount_column"]);
  });

  it("refuses a column used twice, on the later field, ignoring case", () => {
    const twice = { ...VALID, credit_column: "withdrawal" };
    expect(mappingProblems(twice, ["01/08/2026"])).toEqual({
      credit_column: "This column is already used above. Each column can be used once.",
    });
  });

  it("refuses a date format that doesn't read the file's dates", () => {
    expect(mappingProblems(VALID, ["2026-08-01"]).date_format).toMatch(/doesn't match/);
  });
});

describe("toColumnMapping", () => {
  it("sends the pair and the balance for the two-column style", () => {
    expect(toColumnMapping(VALID)).toEqual({
      date_column: "Date",
      description_column: "Narration",
      date_format: "%d/%m/%Y",
      debit_column: "Withdrawal",
      credit_column: "Deposit",
      balance_column: "Balance",
    });
  });

  it("sends only the amount column for the one-column style, and no empty balance", () => {
    // Sending debit/credit too would be refused by the backend ("give either
    // debit_column and credit_column, or amount_column").
    const values = { ...VALID, amountStyle: "one", amount_column: "Amount", balance_column: "" };
    expect(toColumnMapping(values)).toEqual({
      date_column: "Date",
      description_column: "Narration",
      date_format: "%d/%m/%Y",
      amount_column: "Amount",
    });
  });
});
