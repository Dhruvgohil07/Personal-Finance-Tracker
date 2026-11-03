// Dates as the user reads them, in Indian English: "1 Aug 2026".
//
// Two kinds of value arrive from the API, and they need different care:
//
// - A statement DATE, "2026-08-01": a day, with no time and no timezone
//   (CLAUDE.md rule 10). `new Date("2026-08-01")` would read it as midnight
//   UTC and could show 31 July in a timezone behind UTC, so the parts are
//   formatted as a UTC date AND printed in UTC: nothing can shift a day.
// - A TIMESTAMP, "2026-11-03T10:00:00Z": a moment, shown in the app's
//   timezone, Asia/Kolkata, whatever the browser's own timezone is.

const APP_TIME_ZONE = "Asia/Kolkata";

const dateFormat = new Intl.DateTimeFormat("en-IN", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

const dateTimeFormat = new Intl.DateTimeFormat("en-IN", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZone: APP_TIME_ZONE,
});

const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

/** "2026-08-01" → "1 Aug 2026". Anything else is returned unchanged. */
export function formatDate(isoDate) {
  const match = DATE_PATTERN.exec(isoDate ?? "");
  if (!match) {
    return isoDate ?? "";
  }
  const [, year, month, day] = match.map(Number);
  return dateFormat.format(new Date(Date.UTC(year, month - 1, day)));
}

/** "2026-11-03T10:00:00Z" → "3 Nov 2026, 3:30 pm" (Asia/Kolkata). */
export function formatDateTime(isoTimestamp) {
  const moment = new Date(isoTimestamp);
  return Number.isNaN(moment.getTime()) ? "" : dateTimeFormat.format(moment);
}

/** A statement's period, "1 Aug 2026 – 31 Aug 2026", or null if unknown. */
export function formatPeriod(start, end) {
  if (!start || !end) {
    return null;
  }
  return start === end ? formatDate(start) : `${formatDate(start)} – ${formatDate(end)}`;
}

const indiaDayParts = new Intl.DateTimeFormat("en-US", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  timeZone: APP_TIME_ZONE,
});

/** Today's date in India, "2026-11-03", whatever the browser's timezone. */
export function todayInIndia(now = new Date()) {
  // Assembled from the parts, not from a locale's printed order, which
  // differs between locales (and could change with the browser's data).
  const part = (type) => indiaDayParts.formatToParts(now).find((p) => p.type === type).value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/**
 * The first and last day of a calendar month, as ISO dates:
 * `monthRange("2026-11-03", 0)` is `{ from: "2026-11-01", to: "2026-11-30" }`,
 * and `-1` is the month before. Worked out with UTC dates, so no timezone
 * can move a day (see the top of this file).
 */
export function monthRange(isoDate, offset = 0) {
  const [year, month] = isoDate.split("-").map(Number);
  const first = new Date(Date.UTC(year, month - 1 + offset, 1));
  // Day 0 of the NEXT month is the last day of this one.
  const last = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0));
  const iso = (date) => date.toISOString().slice(0, 10);
  return { from: iso(first), to: iso(last) };
}
