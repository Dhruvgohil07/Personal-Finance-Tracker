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
// The WHOLE file is decoded, as the backend does: whether a file is UTF-8
// or Windows-1252 depends on every byte, and a single Windows-1252 byte
// far down the file changes how the backend reads the column names at the
// top (review of PR 34). Files are at most 10 MB and checked before this
// runs. Only the first rows are then split into cells. Nothing here is
// sent anywhere; the file itself is uploaded as it is.

const DELIMITERS = [",", ";", "\t", "|"];
// How many rows the delimiter is judged on: the backend's _SNIFF_ROWS
// (app/parsers/readers.py), which runs this same rule. Keep them in step.
export const SNIFF_ROWS = 60;
// The backend's _MAX_HEADER_SEARCH_ROWS (app/parsers/generic_csv.py).
export const MAX_HEADER_SEARCH_ROWS = 50;
// Rows kept for the mapping: the header search area plus up to 200 data
// rows, whose dates decide between day-first and month-first.
const MAX_PREVIEW_ROWS = MAX_HEADER_SEARCH_ROWS + 200;

/** A file whose first rows can't be read as a CSV; `message` is for the user. */
export class CsvPreviewError extends Error {
  constructor(message) {
    super(message);
    this.name = "CsvPreviewError";
  }
}

const UNREADABLE = "This file isn't readable text. Export the statement as a CSV and try again.";

// Bytes that Python's cp1252 codec REFUSES but the browser's windows-1252
// decoder turns into control characters. The backend fails a file
// containing one ("file is not readable text"), so the browser must too,
// instead of offering a mapping for a file that can't be imported.
const CP1252_UNDEFINED = new Set([0x81, 0x8d, 0x8f, 0x90, 0x9d]);

/**
 * Bytes → text, exactly as the backend's _decode_csv:
 *
 *   starts with a UTF-16 byte-order mark  → UTF-16, refused if broken
 *   valid UTF-8 (all of it)               → UTF-8, a leading BOM dropped
 *   otherwise                             → Windows-1252, refused if it
 *                                           holds a byte cp1252 lacks
 *
 * No real CSV contains a NUL character; finding one means the file was in
 * some other encoding (or isn't text at all).
 */
export function decodeCsvBytes(bytes) {
  let text;
  try {
    if (bytes[0] === 0xff && bytes[1] === 0xfe) {
      text = new TextDecoder("utf-16le", { fatal: true }).decode(bytes);
    } else if (bytes[0] === 0xfe && bytes[1] === 0xff) {
      text = new TextDecoder("utf-16be", { fatal: true }).decode(bytes);
    }
  } catch {
    throw new CsvPreviewError(UNREADABLE);
  }
  if (text === undefined) {
    try {
      // The UTF-8 decoder drops a leading byte-order mark by itself.
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      if (bytes.some((byte) => CP1252_UNDEFINED.has(byte))) {
        throw new CsvPreviewError(UNREADABLE);
      }
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
 * The delimiter that splits the lines most consistently, among , ; tab |.
 *
 * For each candidate: split the first SNIFF_ROWS rows with it and find the
 * most common number of cells (above 1). The winner is the candidate for
 * which the most rows have that common count. Ties go to the earlier
 * candidate, so a file with no delimiter at all (one column) is read with
 * a comma.
 *
 * The backend runs the SAME rule (_sniff_delimiter in
 * app/parsers/readers.py); it used to run Python's csv.Sniffer, which gave
 * up on any file with a title line above the table and fell back to a
 * comma, so a semicolon CSV mapped here could never be read there (review
 * of PR 34). Change one, change both.
 */
export function sniffDelimiter(text) {
  let best = { delimiter: ",", rows: 0 };
  for (const delimiter of DELIMITERS) {
    const counts = new Map();
    for (const row of parseCsv(text, delimiter, SNIFF_ROWS)) {
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
  const bytes = new Uint8Array(await file.arrayBuffer());
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
