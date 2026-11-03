import { afterEach, describe, expect, it, vi } from "vitest";

import { formatDate, formatDateTime, formatPeriod, monthRange, todayInIndia } from "@/lib/dates";

describe("todayInIndia", () => {
  it("is already tomorrow in India when it is late evening in UTC", () => {
    // 20:00 UTC on 3 Nov is 1:30 am on 4 Nov in India.
    expect(todayInIndia(new Date("2026-11-03T20:00:00Z"))).toBe("2026-11-04");
    expect(todayInIndia(new Date("2026-11-03T10:00:00Z"))).toBe("2026-11-03");
  });
});

describe("monthRange", () => {
  it.each([
    ["2026-11-03", 0, { from: "2026-11-01", to: "2026-11-30" }],
    ["2026-11-03", -1, { from: "2026-10-01", to: "2026-10-31" }],
    ["2026-01-15", -1, { from: "2025-12-01", to: "2025-12-31" }], // across a year
    ["2024-03-10", -1, { from: "2024-02-01", to: "2024-02-29" }], // leap year
    ["2026-03-31", -1, { from: "2026-02-01", to: "2026-02-28" }], // from the 31st
  ])("%s with offset %i", (iso, offset, expected) => {
    expect(monthRange(iso, offset)).toEqual(expected);
  });
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("formatDate", () => {
  it.each([
    ["2026-08-01", "1 Aug 2026"],
    ["2026-12-31", "31 Dec 2026"],
    ["2024-02-29", "29 Feb 2024"],
  ])("formats the statement date %s as %s", (iso, expected) => {
    expect(formatDate(iso)).toBe(expected);
  });

  it("never moves a date to the day before, whatever the browser's timezone", () => {
    // Node re-reads the TZ variable when it changes, like a browser in
    // another country would have it.
    vi.stubEnv("TZ", "America/New_York");
    // The trap: new Date("2026-08-01") is midnight UTC, which is still
    // 31 July in New York...
    expect(new Date("2026-08-01").getDate()).toBe(31);
    // ...but formatDate builds and prints the date in UTC, so the
    // browser's own timezone plays no part.
    expect(formatDate("2026-08-01")).toBe("1 Aug 2026");
  });

  it("returns anything that isn't a plain date unchanged", () => {
    expect(formatDate("not a date")).toBe("not a date");
    expect(formatDate(null)).toBe("");
  });
});

describe("formatDateTime", () => {
  it("shows a timestamp in India's time (UTC+5:30)", () => {
    // 10:00 UTC is 3:30 pm in Asia/Kolkata. \s: some ICU versions put a
    // narrow no-break space before "pm".
    expect(formatDateTime("2026-11-03T10:00:00Z")).toMatch(/^3 Nov 2026, 3:30\spm$/);
  });

  it("moves to the next day when it's already past midnight in India", () => {
    // 20:00 UTC on 3 Nov is 1:30 am on 4 Nov in India.
    expect(formatDateTime("2026-11-03T20:00:00Z")).toMatch(/^4 Nov 2026, 1:30\sam$/);
  });

  it("gives an empty string for an invalid timestamp", () => {
    expect(formatDateTime("nonsense")).toBe("");
  });
});

describe("formatPeriod", () => {
  it("joins the first and last day", () => {
    expect(formatPeriod("2026-08-01", "2026-08-31")).toBe("1 Aug 2026 – 31 Aug 2026");
  });

  it("shows a one-day statement once", () => {
    expect(formatPeriod("2026-08-01", "2026-08-01")).toBe("1 Aug 2026");
  });

  it("is null when the period is unknown", () => {
    expect(formatPeriod(null, null)).toBeNull();
  });
});
