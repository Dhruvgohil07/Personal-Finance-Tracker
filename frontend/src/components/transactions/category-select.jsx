// A drop-down of categories, grouped by kind with <optgroup>s (spending,
// income, transfers between your own accounts), so a long list stays
// readable. Used by the filters and by each transaction's row.
//
// `emptyLabel` is the first choice, whose value is "": "All categories" in
// the filters, "Not categorized yet" in a row. `extraOptions` come right
// after it (the filters' "Not categorized yet" choice).

import { CONTROL_CLASSES } from "@/components/form-field";

const KIND_GROUPS = [
  { kind: "expense", label: "Spending" },
  { kind: "income", label: "Income" },
  { kind: "transfer", label: "Between your own accounts" },
];

export function CategorySelect({ categories, emptyLabel, extraOptions = [], className = "", ...props }) {
  return (
    <select className={`${CONTROL_CLASSES} ${className}`} {...props}>
      <option value="">{emptyLabel}</option>
      {extraOptions.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
      {KIND_GROUPS.map(({ kind, label }) => {
        const inGroup = categories.filter((category) => category.kind === kind);
        return inGroup.length > 0 ? (
          <optgroup key={kind} label={label}>
            {inGroup.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </optgroup>
        ) : null;
      })}
    </select>
  );
}
