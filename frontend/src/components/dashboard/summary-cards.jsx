// Three cards for the month: money in, money out, and what was left (or
// overspent), each against the month before. Everything shown comes from
// GET /insights/summary as it is: the amounts as `*_display`, the changes
// as `*_change_pct` (hard constraint 4: no money arithmetic in the browser).
//
// Self transfers are already left out by the API (SPEC §9.1), which the
// small print under the cards says, since a user who moved ₹10,000 to
// their credit card might otherwise look for it here.

import { changeText, monthName } from "@/lib/insights";

function Card({ title, amount, comparison }) {
  return (
    <div className="flex flex-col gap-1 rounded-md border border-neutral-200 p-4 dark:border-neutral-800">
      {/* A <dl> pairs a label with its value for screen readers. */}
      <dt className="text-muted text-sm">{title}</dt>
      <dd className="text-2xl font-semibold tabular-nums">{amount}</dd>
      <dd className="text-muted text-sm">{comparison}</dd>
    </div>
  );
}

export function SummaryCards({ summary }) {
  const { current, previous } = summary;
  const previousName = monthName(previous.month);
  // net_paise is only compared with zero, to choose the words: the
  // amount itself is the API's net_display.
  const overspent = current.net_paise < 0;

  return (
    <div className="flex flex-col gap-2">
      <dl className="grid gap-4 sm:grid-cols-3">
        <Card
          title="Money in"
          amount={current.income_display}
          comparison={changeText(
            summary.income_change_pct,
            previous.month,
            `Nothing came in during ${previousName}`,
          )}
        />
        <Card
          title="Money out"
          amount={current.spend_display}
          comparison={changeText(
            summary.spend_change_pct,
            previous.month,
            `Nothing went out during ${previousName}`,
          )}
        />
        <Card
          title={overspent ? "Overspent" : "Left over"}
          amount={current.net_display}
          comparison={`${previousName}: ${previous.net_display}`}
        />
      </dl>
      <p className="text-muted text-xs">
        Money moved between your own accounts is not counted as money in or out.
      </p>
    </div>
  );
}
