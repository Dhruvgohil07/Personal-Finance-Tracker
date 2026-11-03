// The column mapping of a CSV no bank parser recognises (SPEC §6.2): which
// column is the date, the description, the amount(s) and the balance, and
// how the dates are written. The backend's rules are in
// app/schemas/upload.py (ColumnMappingRequest) and
// app/parsers/generic_csv.py (ColumnMapping); this module checks the same
// rules before sending, so mistakes are shown next to the right field.
//
// The form's values (one object, "" meaning "not chosen"):
//   { headerIndex, amountStyle: "two" | "one", date_column,
//     description_column, debit_column, credit_column, amount_column,
//     balance_column, date_format }

import { formatDate } from "@/lib/dates";

export const COLUMN_FIELDS = [
  "date_column",
  "description_column",
  "debit_column",
  "credit_column",
  "amount_column",
  "balance_column",
];

// ---- the file's columns ------------------------------------------------

/**
 * The column names of a header row: trimmed, non-empty, each once.
 *
 * The backend matches names ignoring case and finds the FIRST column with
 * a name (generic_csv.py, _find_header), so a second "Amount" column can
 * never be chosen; offering it would only mislead.
 */
export function columnLabels(headerRow) {
  const seen = new Set();
  const labels = [];
  for (const cell of headerRow) {
    const label = cell.trim();
    const key = label.toLowerCase();
    if (label !== "" && !seen.has(key)) {
      seen.add(key);
      labels.push(label);
    }
  }
  return labels;
}

/**
 * The data rows under the header, as the backend reads them: rows whose
 * date cell is empty (blank lines, footers, wrapped narrations) are skipped.
 * Each row is returned as a function `cell(label)`: the trimmed text under
 * that column name ("" for no label), found the way the backend finds it.
 */
export function dataRows(rows, headerIndex, dateColumn, limit = 5) {
  const header = rows[headerIndex] ?? [];
  // label (lower case) -> the index of the FIRST column with that name
  const indexOf = new Map();
  header.forEach((cell, index) => {
    const key = cell.trim().toLowerCase();
    if (key !== "" && !indexOf.has(key)) {
      indexOf.set(key, index);
    }
  });
  const cellOf = (row, label) => (row[indexOf.get(label.toLowerCase())] ?? "").trim();

  const result = [];
  for (const row of rows.slice(headerIndex + 1)) {
    if (result.length >= limit) {
      break;
    }
    if (dateColumn && cellOf(row, dateColumn) !== "") {
      result.push((label) => (label ? cellOf(row, label) : ""));
    }
  }
  return result;
}

// ---- guessing from the names ------------------------------------------

// For each role, patterns from the most to the least specific. The first
// pattern that matches an unused label wins. Only a suggestion: the user
// sees every choice and can change it.
const GUESSES = {
  date_column: [/^(transaction|txn|tran)\.?\s*date$/i, /^date$/i, /^(posting|book)\s*date$/i, /date/i],
  description_column: [/narration|description|particulars|remarks|details/i],
  debit_column: [/withdrawal|debit|^dr\.?$|money out|paid out/i],
  credit_column: [/deposit|credit|^cr\.?$|money in|paid in/i],
  amount_column: [/^(transaction\s*)?(amount|amt)\b/i],
  balance_column: [/balance/i],
};

/** Pre-filled form values for these column names; the user confirms them. */
export function guessMapping(labels, headerIndex) {
  const used = new Set();
  const pick = (role) => {
    for (const pattern of GUESSES[role]) {
      const label = labels.find((candidate) => !used.has(candidate) && pattern.test(candidate));
      if (label) {
        used.add(label);
        return label;
      }
    }
    return "";
  };

  const date_column = pick("date_column");
  const description_column = pick("description_column");
  const debit_column = pick("debit_column");
  const credit_column = pick("credit_column");
  // Two columns only if BOTH were found; otherwise try one signed column.
  const twoColumns = debit_column !== "" && credit_column !== "";
  if (!twoColumns) {
    used.delete(debit_column);
    used.delete(credit_column);
  }
  const amount_column = twoColumns ? "" : pick("amount_column");
  return {
    headerIndex,
    amountStyle: twoColumns || amount_column === "" ? "two" : "one",
    date_column,
    description_column,
    debit_column: twoColumns ? debit_column : "",
    credit_column: twoColumns ? credit_column : "",
    amount_column,
    balance_column: pick("balance_column"),
    date_format: "",
  };
}

// ---- dates ---------------------------------------------------------------

// The formats offered, with how 1 August 2026 looks in each. They are
// Python strptime patterns because that is what the backend runs; each one
// passes the backend's own check (it prints and re-reads a whole date).
export const DATE_FORMATS = [
  { pattern: "%d/%m/%Y", example: "01/08/2026" },
  { pattern: "%d-%m-%Y", example: "01-08-2026" },
  { pattern: "%d.%m.%Y", example: "01.08.2026" },
  { pattern: "%d/%m/%y", example: "01/08/26" },
  { pattern: "%d-%m-%y", example: "01-08-26" },
  { pattern: "%d-%b-%Y", example: "01-Aug-2026" },
  { pattern: "%d %b %Y", example: "01 Aug 2026" },
  { pattern: "%d-%b-%y", example: "01-Aug-26" },
  { pattern: "%d %B %Y", example: "01 August 2026" },
  { pattern: "%Y-%m-%d", example: "2026-08-01" },
  { pattern: "%m/%d/%Y", example: "08/01/2026 (month first, US style)" },
];

const MONTHS = [
  "january",
  "february",
  "march",
  "april",
  "may",
  "june",
  "july",
  "august",
  "september",
  "october",
  "november",
  "december",
];

// Each directive as Python's _strptime writes its regular expression, so a
// cell matches here exactly when it matches on the server: %d and %m take
// one or two digits, %Y exactly four, %y exactly two, and month names
// ignore case.
const DIRECTIVES = {
  d: "(3[01]|[12]\\d|0[1-9]|[1-9]| [1-9])",
  m: "(1[0-2]|0[1-9]|[1-9])",
  Y: "(\\d{4})",
  y: "(\\d{2})",
  b: `(${MONTHS.map((month) => month.slice(0, 3)).join("|")})`,
  B: `(${MONTHS.join("|")})`,
};

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/**
 * Read `text` with a strptime `pattern`, as the backend will:
 * `"2026-08-01"`-style ISO date, or null when it doesn't match.
 *
 * Supports the directives DATE_FORMATS uses (%d %m %Y %y %b %B). Like
 * strptime: the WHOLE cell must match (after trimming, which the backend
 * does too), a space in the pattern matches any run of spaces, and an
 * impossible date (31 June) is refused. Two-digit years: 69-99 are the
 * 1900s and 00-68 the 2000s, Python's rule.
 */
export function parseDateWith(text, pattern) {
  const parts = [];
  let source = "";
  for (let i = 0; i < pattern.length; i++) {
    if (pattern[i] === "%" && DIRECTIVES[pattern[i + 1]]) {
      parts.push(pattern[i + 1]);
      source += DIRECTIVES[pattern[i + 1]];
      i++;
    } else if (/\s/.test(pattern[i])) {
      source += "\\s+";
    } else {
      source += escapeRegExp(pattern[i]);
    }
  }
  const match = new RegExp(`^${source}$`, "i").exec(text.trim());
  if (!match) {
    return null;
  }

  let day;
  let month;
  let year;
  parts.forEach((part, index) => {
    const value = match[index + 1].trim().toLowerCase();
    if (part === "d") day = Number(value);
    if (part === "m") month = Number(value);
    if (part === "Y") year = Number(value);
    if (part === "y") year = Number(value) + (Number(value) >= 69 ? 1900 : 2000);
    if (part === "b") month = MONTHS.findIndex((name) => name.startsWith(value)) + 1;
    if (part === "B") month = MONTHS.indexOf(value) + 1;
  });

  // Date.UTC rolls 31 June over to 1 July; a real date survives unchanged.
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) {
    return null;
  }
  const pad = (number) => String(number).padStart(2, "0");
  return `${year}-${pad(month)}-${pad(day)}`;
}

/** The DATE_FORMATS patterns that read every one of `samples`. */
export function formatsFitting(samples) {
  if (samples.length === 0) {
    return [];
  }
  return DATE_FORMATS.map((format) => format.pattern).filter((pattern) =>
    samples.every((sample) => parseDateWith(sample, pattern) !== null),
  );
}

/** The date cells of the first data rows, for checking a format against. */
export function dateSamples(rows, values) {
  return dataRows(rows, values.headerIndex, values.date_column).map((cell) =>
    cell(values.date_column),
  );
}

/**
 * `values` with a date format filled in, when it is still empty and
 * exactly ONE format reads the file's first dates. With "01/08/2026",
 * both day-first and month-first fit, and the user must choose: 1 August
 * or 8 January is not something to guess (the backend's own rule).
 */
export function suggestDateFormat(values, rows) {
  if (values.date_format !== "") {
    return values;
  }
  const fitting = formatsFitting(dateSamples(rows, values));
  return fitting.length === 1 ? { ...values, date_format: fitting[0] } : values;
}

/** "1 Aug 2026" for a date cell read with `pattern`, or null. */
export function previewDate(text, pattern) {
  const iso = pattern ? parseDateWith(text, pattern) : null;
  return iso ? formatDate(iso) : null;
}

// ---- checking and sending ----------------------------------------------

/** The columns this mapping uses, in the backend's order. */
function usedColumns(values) {
  const amountFields =
    values.amountStyle === "one" ? ["amount_column"] : ["debit_column", "credit_column"];
  return ["date_column", "description_column", ...amountFields, "balance_column"].filter(
    (field) => values[field] !== "",
  );
}

/**
 * `{ field: "message" }` for everything the backend would refuse, plus a
 * date format that doesn't read the file's own dates (which the backend
 * would only find while importing, failing the whole upload). Empty when
 * the mapping can be sent.
 */
export function mappingProblems(values, dateSamples = []) {
  const problems = {};
  if (!values.date_column) problems.date_column = "Choose the column with the dates.";
  if (!values.description_column) {
    problems.description_column = "Choose the column that describes each transaction.";
  }
  if (values.amountStyle === "one") {
    if (!values.amount_column) problems.amount_column = "Choose the column with the amounts.";
  } else {
    if (!values.debit_column) problems.debit_column = "Choose the column for money going out.";
    if (!values.credit_column) problems.credit_column = "Choose the column for money coming in.";
  }

  // Each column may be used once (the backend refuses a mapping that uses
  // one twice). Reported on the later field, the one just changed most
  // likely.
  const seen = new Set();
  for (const field of usedColumns(values)) {
    const key = values[field].toLowerCase();
    if (seen.has(key) && !problems[field]) {
      problems[field] = "This column is already used above. Each column can be used once.";
    }
    seen.add(key);
  }

  if (!values.date_format) {
    problems.date_format = "Choose how the dates are written.";
  } else if (dateSamples.some((sample) => parseDateWith(sample, values.date_format) === null)) {
    problems.date_format = "This doesn't match the dates in your file. Check the preview below.";
  }
  return problems;
}

/** The `column_mapping` object the API takes, from valid form values. */
export function toColumnMapping(values) {
  const mapping = {
    date_column: values.date_column,
    description_column: values.description_column,
    date_format: values.date_format,
  };
  if (values.amountStyle === "one") {
    mapping.amount_column = values.amount_column;
  } else {
    mapping.debit_column = values.debit_column;
    mapping.credit_column = values.credit_column;
  }
  if (values.balance_column) {
    mapping.balance_column = values.balance_column;
  }
  return mapping;
}
