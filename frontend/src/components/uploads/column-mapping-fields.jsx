"use client";

// "Which column is which?" for a CSV: shown inside the upload form once a
// CSV is chosen (Phase 3 Step 7). Every choice is pre-filled from the
// file's own column names (guessMapping) for the user to confirm, and a
// preview shows the first rows as Kharcha will read them.
//
// A CONTROLLED component: the parent form holds `values` and gets every
// change through `onChange(newValues)`, because the form needs them to
// check and send the upload. This component only draws them.

import { FormField } from "@/components/form-field";
import {
  DATE_FORMATS,
  columnLabels,
  dataRows,
  dateSamples,
  formatsFitting,
  guessMapping,
  previewDate,
  suggestDateFormat,
} from "@/lib/column-mapping";
import { MAX_HEADER_SEARCH_ROWS } from "@/lib/csv-preview";

const AMOUNT_STYLES = [
  { value: "two", label: "Two columns: money out and money in" },
  { value: "one", label: "One column: a minus sign means money out" },
];

const PREVIEW_ROWS = 3;

// "Row 4: Date, Narration, Withdrawal…": what the header-row choice shows.
function rowSummary(row, index) {
  const text = row
    .map((cell) => cell.trim())
    .filter(Boolean)
    .join(", ");
  return `Row ${index + 1}: ${text.length > 60 ? `${text.slice(0, 60)}…` : text}`;
}

export function ColumnMappingFields({ rows, values, onChange, errors = {} }) {
  const labels = columnLabels(rows[values.headerIndex] ?? []);
  const columnOptions = (emptyLabel) => [
    { value: "", label: emptyLabel },
    ...labels.map((label) => ({ value: label, label })),
  ];

  // Rows that could hold the column names: two or more cells filled in.
  const headerOptions = rows
    .slice(0, MAX_HEADER_SEARCH_ROWS)
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => row.filter((cell) => cell.trim()).length >= 2)
    .map(({ row, index }) => ({ value: String(index), label: rowSummary(row, index) }));

  const samples = dateSamples(rows, values);
  const fitting = formatsFitting(samples);
  const formatOptions = [
    { value: "", label: "Choose a format" },
    ...DATE_FORMATS.map(({ pattern, example }) => ({
      value: pattern,
      label:
        samples.length > 0 && !fitting.includes(pattern)
          ? `${example} (doesn't match this file)`
          : example,
    })),
  ];

  function update(field) {
    return (event) => {
      const next = { ...values, [field]: event.target.value };
      // A new date column has other dates: suggest a format for those.
      onChange(field === "date_column" ? suggestDateFormat({ ...next, date_format: "" }, rows) : next);
    };
  }

  function changeHeaderRow(event) {
    const headerIndex = Number(event.target.value);
    // Other column names: guess everything again from them.
    onChange(suggestDateFormat(guessMapping(columnLabels(rows[headerIndex]), headerIndex), rows));
  }

  const twoColumns = values.amountStyle === "two";

  return (
    <fieldset className="flex flex-col gap-4 rounded-md border border-neutral-200 p-4 dark:border-neutral-800">
      <legend className="px-1 text-sm font-semibold">Which column is which?</legend>
      <p className="text-muted text-xs">
        A CSV can come from any bank, so tell Kharcha where to find each part of a transaction.
        The choices below are guesses from your file&apos;s column names; check them against the
        preview.
      </p>
      {errors.column_mapping ? (
        <p className="text-sm text-red-600 dark:text-red-400">{errors.column_mapping}</p>
      ) : null}
      <FormField
        label="Column names are on"
        name="header_row"
        options={headerOptions}
        value={String(values.headerIndex)}
        onChange={changeHeaderRow}
      />
      <FormField
        label="Date column"
        name="date_column"
        options={columnOptions("Choose a column")}
        value={values.date_column}
        onChange={update("date_column")}
        error={errors.date_column}
      />
      <FormField
        label="Date format"
        name="date_format"
        options={formatOptions}
        value={values.date_format}
        onChange={update("date_format")}
        hint={
          samples.length > 0
            ? `Your file's first date is ${samples[0]}. Is that day first or month first? Check the preview.`
            : undefined
        }
        error={errors.date_format}
      />
      <FormField
        label="Description column"
        name="description_column"
        options={columnOptions("Choose a column")}
        value={values.description_column}
        onChange={update("description_column")}
        error={errors.description_column}
      />
      <FormField
        label="How are amounts written?"
        name="amount_style"
        options={AMOUNT_STYLES}
        value={values.amountStyle}
        onChange={update("amountStyle")}
      />
      {twoColumns ? (
        <>
          <FormField
            label="Money out column"
            name="debit_column"
            options={columnOptions("Choose a column")}
            value={values.debit_column}
            onChange={update("debit_column")}
            hint="Often called Withdrawal or Debit."
            error={errors.debit_column}
          />
          <FormField
            label="Money in column"
            name="credit_column"
            options={columnOptions("Choose a column")}
            value={values.credit_column}
            onChange={update("credit_column")}
            hint="Often called Deposit or Credit."
            error={errors.credit_column}
          />
        </>
      ) : (
        <FormField
          label="Amount column"
          name="amount_column"
          options={columnOptions("Choose a column")}
          value={values.amount_column}
          onChange={update("amount_column")}
          error={errors.amount_column}
        />
      )}
      <FormField
        label="Balance column (optional)"
        name="balance_column"
        options={columnOptions("No balance column")}
        value={values.balance_column}
        onChange={update("balance_column")}
        hint="With a balance, Kharcha can check that no transaction was missed."
        error={errors.balance_column}
      />
      <MappingPreview rows={rows} values={values} />
    </fieldset>
  );
}

/**
 * The first rows as the mapping reads them. Amounts are shown as the file
 * writes them (text, never converted to a number: hard constraint 4); the
 * date is shown READ, so "1 Aug 2026" vs "8 Jan 2026" shows at a glance
 * whether the format is right.
 */
function MappingPreview({ rows, values }) {
  if (!values.date_column) {
    return null;
  }
  const preview = dataRows(rows, values.headerIndex, values.date_column, PREVIEW_ROWS);
  if (preview.length === 0) {
    return (
      <p className="text-muted text-sm">
        No rows with a date were found under these column names.
      </p>
    );
  }

  const twoColumns = values.amountStyle === "two";
  const amountHeads = twoColumns ? ["Money out", "Money in"] : ["Amount"];
  const amountFields = twoColumns ? ["debit_column", "credit_column"] : ["amount_column"];

  return (
    <div className="flex flex-col gap-1">
      <p className="text-sm font-medium">Preview: the first {preview.length} rows</p>
      {/* overflow-x-auto: the table scrolls inside itself on a phone
          instead of widening the page. */}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="text-muted">
              <th className="py-1 pr-3 font-medium">Date</th>
              <th className="py-1 pr-3 font-medium">Description</th>
              {amountHeads.map((head) => (
                <th key={head} className="py-1 pr-3 font-medium">
                  {head}
                </th>
              ))}
              {values.balance_column ? <th className="py-1 font-medium">Balance</th> : null}
            </tr>
          </thead>
          <tbody>
            {preview.map((cell, index) => {
              const rawDate = cell(values.date_column);
              const readDate = previewDate(rawDate, values.date_format);
              return (
                <tr key={index} className="border-t border-neutral-200 dark:border-neutral-800">
                  <td className="py-1 pr-3 whitespace-nowrap">
                    {readDate ?? (
                      <span className="text-red-700 dark:text-red-400">{rawDate} (?)</span>
                    )}
                  </td>
                  <td className="py-1 pr-3">{cell(values.description_column)}</td>
                  {amountFields.map((field) => (
                    <td key={field} className="py-1 pr-3 whitespace-nowrap">
                      {cell(values[field])}
                    </td>
                  ))}
                  {values.balance_column ? (
                    <td className="py-1 whitespace-nowrap">{cell(values.balance_column)}</td>
                  ) : null}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
