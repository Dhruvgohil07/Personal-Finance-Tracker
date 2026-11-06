"use client";

// "Where it went": the month's spending per category, as a donut chart and
// as a list. The data is GET /insights/categories: debits only, self
// transfers left out, biggest first, with the uncategorized rows as one
// item (`category_id: null`) so the parts add up to the total.
//
// The chart is drawn by Recharts, a React charting library: you describe
// the chart as components (<PieChart> holding a <Pie>, one <Cell> per
// slice) and it draws the SVG. A donut is a pie with a hole
// (`innerRadius`).
//
// The LIST is the real content; the chart is a picture of it. A chart
// can't be read by a screen reader, and thin slices can't be told apart
// by colour alone, so every slice is also a row with its name, amount,
// share and count, which links to those transactions. The chart is
// therefore hidden from screen readers (aria-hidden) and from the keyboard
// (`accessibilityLayer={false}`), so nobody meets the same data twice.
//
// A fixed-size chart, not Recharts' ResponsiveContainer: a donut has no
// reason to stretch, and ResponsiveContainer measures its parent, which
// is 0 × 0 before the browser lays the page out (and always, in tests).

import Link from "next/link";
import { Cell, Pie, PieChart, Tooltip } from "recharts";

import {
  categoryTransactionsLink,
  formatPercent,
  sliceColor,
} from "@/lib/insights";
import { plural } from "@/lib/text";

const CHART_SIZE = 200;

// What the chart shows on hover: the category and its amount, both as the
// API wrote them. Recharts passes `payload`, the hovered slice's data.
function SliceTooltip({ active, payload }) {
  if (!active || !payload?.length) {
    return null;
  }
  const item = payload[0].payload;
  return (
    <div className="bg-background rounded-md border border-neutral-200 px-2 py-1 text-sm shadow dark:border-neutral-800">
      {item.name}: {item.amount_display}
    </div>
  );
}

export function CategoryBreakdown({ breakdown, month }) {
  return (
    <div className="flex flex-col items-center gap-6 md:flex-row md:items-start">
      <div aria-hidden="true" className="shrink-0">
        <PieChart width={CHART_SIZE} height={CHART_SIZE} accessibilityLayer={false}>
          <Pie
            data={breakdown.items}
            dataKey="amount_paise"
            nameKey="name"
            innerRadius="60%"
            outerRadius="100%"
            // No animation: the numbers are what matter, and a chart that
            // grows on every month change only delays reading them.
            isAnimationActive={false}
            stroke="var(--background)"
          >
            {breakdown.items.map((item) => (
              <Cell key={item.category_id ?? "none"} fill={sliceColor(item)} />
            ))}
          </Pie>
          <Tooltip content={<SliceTooltip />} />
        </PieChart>
      </div>

      <ul aria-label="Spending by category" className="flex w-full flex-col">
        {breakdown.items.map((item) => (
          <li
            key={item.category_id ?? "none"}
            className="border-b border-neutral-200 last:border-b-0 dark:border-neutral-800"
          >
            <Link
              href={categoryTransactionsLink(item, month)}
              className="flex items-center gap-3 px-1 py-2 hover:bg-neutral-100 dark:hover:bg-neutral-900"
            >
              <span
                aria-hidden="true"
                className="size-3 shrink-0 rounded-full"
                style={{ backgroundColor: sliceColor(item) }}
              />
              <span className="flex flex-1 flex-col">
                <span className="font-medium">{item.name}</span>
                <span className="text-muted text-xs">{plural(item.transaction_count, "transaction")}</span>
              </span>
              <span className="flex flex-col items-end">
                <span className="tabular-nums">{item.amount_display}</span>
                <span className="text-muted text-xs tabular-nums">
                  {formatPercent(item.share_pct)}
                </span>
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
