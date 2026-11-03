// "‹ October 2026 ›": which month the dashboard shows. The parent owns the
// month (it lives in the URL); this only asks for the one before or after.
//
// The month after the current one can't be chosen: nothing can have
// happened in it yet. The button is disabled rather than hidden, so the
// name of the month doesn't jump sideways when it appears.

import { Button } from "@/components/button";
import { monthLabel, shiftMonth } from "@/lib/insights";

export function MonthPicker({ month, today, onChange }) {
  const previous = shiftMonth(month, -1);
  const next = shiftMonth(month, 1);
  return (
    <div className="flex items-center gap-3">
      <Button
        variant="secondary"
        onClick={() => onChange(previous)}
        aria-label={`Show ${monthLabel(previous)}`}
      >
        ‹
      </Button>
      {/* aria-live: a screen reader hears the new month after a click. */}
      <h2 aria-live="polite" className="min-w-40 text-center text-lg font-semibold">
        {monthLabel(month)}
      </h2>
      <Button
        variant="secondary"
        onClick={() => onChange(next)}
        disabled={month >= today}
        aria-label={`Show ${monthLabel(next)}`}
      >
        ›
      </Button>
    </div>
  );
}
