// Reads the first rows of a CSV in the browser, so the upload form can offer
// the file's own column names for the column mapping (Phase 3 Step 7).
//
// It reads the file the same way the backend does (app/parsers/readers.py),
// so the names offered here are the names the backend will look for:
//
//   decoding   UTF-16 if the file starts with its byte-order mark, else
//              UTF-8 (Excel's BOM swallowed), else Windows-1252
//   delimiter  the one of  ,  ;  tab  |  that splits the lines consistently
//   quoting    "…" around a cell, "" inside it for a quote; a quoted cell
//              may contain the delimiter or a line break
//   header     searched in the first 50 rows, like the backend
//
// Only the first 64 KB are read: the column names and a few rows to preview
// are near the top, and a statement can be megabytes long. Nothing here is
// sent anywhere; the file itself is uploaded as it is.

const PREVIEW_BYTES = 64 * 1024;
const DELIMITERS = [",", ";", "\t", "|"];
// The backend's _MAX_HEADER_SEARCH_ROWS (app/parsers/generic_csv.py).
export const MAX_HEADER_SEARCH_ROWS = 50;
// Rows kept for the preview: the header search area plus a few data rows.
const MAX_PREVIEW_ROWS = MAX_HEADER_SEARCH_ROWS + 10;

/** A file whose first rows can't be read as a CSV; `message` is for the user. */
export class CsvPreviewError extends Error {
  constructor(message) {
    super(message);
    this.name = "CsvPreviewError";
  }
}

const UNREADABLE = "This file isn't readable text. Export the statement as a CSV and try again.";

/**
 * Bytes → text, as the backend's _decode_csv does.
 *
 * `stream: true` matters because only the START of the file is decoded:
 * the 64 KB cut can fall inside a multi-byte UTF-8 character, and a
 * streaming decoder keeps those last bytes back instead of reporting them
 * as invalid (which would wrongly fall back to Windows-1252).
 *
 * No real CSV contains a NUL character; finding one means the file was in
 * some other encoding (or isn't text at all), and reading it on would show
 * garbage column names.
 */
export function decodeCsvBytes(bytes) {
  let text;
  if (bytes[0] === 0xff && bytes[1] === 0xfe) {
    text = new TextDecoder("utf-16le").decode(bytes, { stream: true });
  } else if (bytes[0] === 0xfe && bytes[1] === 0xff) {
    text = new TextDecoder("utf-16be").decode(bytes, { stream: true });
  } else {
    try {
      // The UTF-8 decoder drops a leading byte-order mark by itself.
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes, { stream: true });
    } catch {
      text = new TextDecoder("windows-1252").decode(bytes);
    }
  }
  if (text.includes("\u0000")) {
    throw new CsvPreviewError(UNREADABLE);
  }
  return text;
}

/**
 * Split CSV text into rows of cells (RFC 4180 quoting), at most `maxRows`.
 *
 * A small state machine, one character at a time: outside quotes, the
 * delimiter ends a cell and a line break ends a row; inside quotes, both
 * are ordinary characters, and "" stands for one quote.
 */
export function parseCsv(text, delimiter, maxRows = Infinity) {
  const rows = [];
  let row = [];
  let cell = "";
  let inQuotes = false;

  for (let i = 0; i < text.length && rows.length < maxRows; i++) {
    const char = text[i];
    if (inQuotes) {
      if (char === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (char === '"') {
        inQuotes = false;
      } else {
        cell += char;
      }
    } else if (char === '"' && cell === "") {
      inQuotes = true;
    } else if (char === delimiter) {
      row.push(cell);
      cell = "";
    } else if (char === "\n" || char === "\r") {
      // "\r\n" (Windows) is one line break, not two.
      if (char === "\r" && text[i + 1] === "\n") {
        i++;
      }
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += char;
    }
  }
  // The last line, if the text doesn't end with a line break.
  if (rows.length < maxRows && (cell !== "" || row.length > 0)) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

/**
 * The delimiter that splits the lines most consistently, like Python's
 * csv.Sniffer restricted to the backend's , ; tab |.
 *
 * For each candidate: split the first rows with it and find the most
 * common number of cells (above 1). The winner is the candidate for which
 * the most rows have that common count. Ties go to the earlier candidate,
 * so a file with no delimiter at all (one column) is read with a comma,
 * the backend's fallback too.
 */
export function sniffDelimiter(text) {
  let best = { delimiter: ",", rows: 0 };
  for (const delimiter of DELIMITERS) {
    const counts = new Map();
    for (const row of parseCsv(text, delimiter, MAX_PREVIEW_ROWS)) {
      if (row.length > 1) {
        counts.set(row.length, (counts.get(row.length) ?? 0) + 1);
      }
    }
    const consistentRows = Math.max(0, ...counts.values());
    if (consistentRows > best.rows) {
      best = { delimiter, rows: consistentRows };
    }
  }
  return best.delimiter;
}

// A header cell names a column; it has letters and no digits. Data cells
// (dates, amounts, balances) have digits, and a bank's title or account
// summary above the table fills only one or two cells.
const looksLikeLabel = (cell) => /\p{L}/u.test(cell) && !/\d/.test(cell);

/**
 * The index of the most likely header row, or -1.
 *
 * The row with the most label-like cells among the first 50 (the first
 * such row on a tie), with at least two of them. The user can pick another
 * row in the form when this guess is wrong.
 */
export function findHeaderRow(rows) {
  let bestIndex = -1;
  let bestScore = 1;
  rows.slice(0, MAX_HEADER_SEARCH_ROWS).forEach((row, index) => {
    const score = row.filter((cell) => looksLikeLabel(cell.trim())).length;
    if (score > bestScore) {
      bestIndex = index;
      bestScore = score;
    }
  });
  return bestIndex;
}

/**
 * Read the first rows of a CSV `File`: `{ rows, headerIndex }`.
 * Throws CsvPreviewError with a message for the user when it can't.
 */
export async function readCsvPreview(file) {
  const bytes = new Uint8Array(await file.slice(0, PREVIEW_BYTES).arrayBuffer());
  const text = decodeCsvBytes(bytes);
  const rows = parseCsv(text, sniffDelimiter(text), MAX_PREVIEW_ROWS);
  const headerIndex = findHeaderRow(rows);
  if (headerIndex === -1) {
    throw new CsvPreviewError(
      "We couldn't find the column names in this file. Check that it is a statement exported as CSV.",
    );
  }
  return { rows, headerIndex };
}
