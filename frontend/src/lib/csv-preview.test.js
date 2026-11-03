import { describe, expect, it } from "vitest";

import {
  CsvPreviewError,
  decodeCsvBytes,
  findHeaderRow,
  parseCsv,
  readCsvPreview,
  sniffDelimiter,
} from "@/lib/csv-preview";

const utf8 = (text) => new TextEncoder().encode(text);

describe("decodeCsvBytes", () => {
  it("reads UTF-8 and drops Excel's byte-order mark", () => {
    // Without that, the first column would be called "﻿Date" and never
    // match the "Date" the backend looks for.
    const bytes = new Uint8Array([0xef, 0xbb, 0xbf, ...utf8("Date,Narration")]);
    expect(decodeCsvBytes(bytes)).toBe("Date,Narration");
  });

  it("falls back to Windows-1252, what Excel on Windows writes", () => {
    // 0xE9 is "é" in Windows-1252 and invalid on its own in UTF-8.
    const bytes = new Uint8Array([...utf8("Caf"), 0xe9, ...utf8(",Amount")]);
    expect(decodeCsvBytes(bytes)).toBe("Café,Amount");
  });

  it("reads UTF-16 when the file starts with its byte-order mark", () => {
    const text = "Date\tAmount";
    const bytes = new Uint8Array(2 + text.length * 2);
    bytes.set([0xff, 0xfe]);
    [...text].forEach((char, index) => {
      bytes[2 + index * 2] = char.charCodeAt(0);
    });
    expect(decodeCsvBytes(bytes)).toBe("Date\tAmount");
  });

  it("stays UTF-8 when the 64 KB cut falls inside a character", () => {
    // "₹" is three bytes; keep only the first: a cut, not bad UTF-8.
    const bytes = new Uint8Array([...utf8("Amount ₹,Café")].slice(0, 8));
    expect(decodeCsvBytes(bytes)).toBe("Amount ");
  });

  it("refuses text with NUL characters (another encoding, or not text)", () => {
    expect(() => decodeCsvBytes(new Uint8Array([0x44, 0x00, 0x61, 0x00]))).toThrow(
      CsvPreviewError,
    );
  });
});

describe("parseCsv", () => {
  it("splits lines and cells", () => {
    expect(parseCsv("a,b\r\nc,d\n", ",")).toEqual([
      ["a", "b"],
      ["c", "d"],
    ]);
  });

  it("keeps a quoted delimiter, quote and line break inside one cell", () => {
    expect(parseCsv('"Withdrawal, INR","say ""hi""","two\nlines"', ",")).toEqual([
      ["Withdrawal, INR", 'say "hi"', "two\nlines"],
    ]);
  });

  it("reads a last line without a line break, and keeps empty cells", () => {
    expect(parseCsv("a,,c\nd,e,", ",")).toEqual([
      ["a", "", "c"],
      ["d", "e", ""],
    ]);
  });

  it("stops after maxRows", () => {
    expect(parseCsv("1\n2\n3\n", ",", 2)).toEqual([["1"], ["2"]]);
  });
});

describe("sniffDelimiter", () => {
  it("finds a semicolon even when the narrations contain commas", () => {
    const text = "Date;Narration;Amount\n01/08/2026;UPI, SHOP, PAY;-70,00\n02/08/2026;SALARY;500,00\n";
    expect(sniffDelimiter(text)).toBe(";");
  });

  it("finds a tab", () => {
    expect(sniffDelimiter("Date\tNarration\tAmount\n01/08/2026\tSHOP\t70\n")).toBe("\t");
  });

  it("finds a pipe", () => {
    expect(sniffDelimiter("Date|Narration|Amount\n01/08/2026|SHOP|70\n")).toBe("|");
  });

  it("falls back to a comma for a file with one column, like the backend", () => {
    expect(sniffDelimiter("just\none\ncolumn\n")).toBe(",");
  });
});

describe("findHeaderRow", () => {
  it("skips the bank's title and account summary above the table", () => {
    const rows = [
      ["Statement of account"],
      ["Account Number", "XXXX1234"],
      ["Period", "01/08/2026 to 31/08/2026"],
      [""],
      ["Date", "Narration", "Withdrawal", "Deposit", "Balance"],
      ["01/08/2026", "UPI SHOP", "70.00", "", "1000.00"],
    ];
    expect(findHeaderRow(rows)).toBe(4);
  });

  it("accepts column names with brackets and dots", () => {
    const rows = [["Txn Date", "Chq./Ref.No.", "Withdrawal Amt (INR)"]];
    expect(findHeaderRow(rows)).toBe(0);
  });

  it("is -1 when no row has two or more column names", () => {
    expect(findHeaderRow([["Statement"], ["01/08/2026", "70.00"]])).toBe(-1);
  });

  it("looks only at the first 50 rows, like the backend", () => {
    const rows = [...Array.from({ length: 50 }, () => ["note"]), ["Date", "Narration", "Amount"]];
    expect(findHeaderRow(rows)).toBe(-1);
  });
});

describe("readCsvPreview", () => {
  it("reads a File's rows and finds its header", async () => {
    const file = new File(
      ["﻿My Bank\nDate;Narration;Amount\n01/08/2026;SHOP;-70\n"],
      "statement.csv",
      { type: "text/csv" },
    );
    const { rows, headerIndex } = await readCsvPreview(file);
    expect(headerIndex).toBe(1);
    expect(rows[1]).toEqual(["Date", "Narration", "Amount"]);
  });

  it("explains a file without column names", async () => {
    const file = new File(["12345\n67890\n"], "numbers.csv");
    await expect(readCsvPreview(file)).rejects.toThrow("We couldn't find the column names");
  });
});
