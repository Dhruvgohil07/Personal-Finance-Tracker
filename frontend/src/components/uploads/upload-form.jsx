"use client";

// The "upload a statement" form, used on the uploads page and as the
// first-run step on the home page.
//
// The file input is UNCONTROLLED: a browser never lets a page set a file
// input's value (that would let a page pick files off your disk), so React
// can't hold it in state the way it holds a text field. The chosen File is
// copied into state from onChange, and to empty the input after an upload
// the input is re-created by changing its `key`.
//
// A CSV gets one more step (Phase 3 Step 7): its first rows are read in the
// browser and the user says which column is which (ColumnMappingFields).
// The mapping goes with the upload as `column_mapping`.

import { useRef, useState } from "react";

import { Button } from "@/components/button";
import { FormError } from "@/components/form-error";
import { FormField } from "@/components/form-field";
import { ColumnMappingFields } from "@/components/uploads/column-mapping-fields";
import {
  COLUMN_FIELDS,
  columnLabels,
  dateSamples,
  guessMapping,
  mappingProblems,
  suggestDateFormat,
  toColumnMapping,
} from "@/lib/column-mapping";
import { CsvPreviewError, readCsvPreview } from "@/lib/csv-preview";
import { formErrorsFrom } from "@/lib/form-errors";
import {
  ACCEPTED_FILE_TYPES,
  STATEMENT_PASSWORD_AUTOCOMPLETE,
  MAX_UPLOAD_MB,
  fileProblem,
  isCsv,
  isPdf,
  isUnfinished,
  useCreateUpload,
  useUploads,
} from "@/lib/uploads";

const NO_ERRORS = { fieldErrors: {}, formError: null };

// The fields a 422 can name. The backend names a mapping field
// "column_mapping.date_format" (or just "column_mapping" for a rule about
// the whole mapping); the form shows those under its own field names.
const MAPPING_PREFIX = "column_mapping.";
const FIELDS = [
  "file",
  "account_id",
  "password",
  "column_mapping",
  ...[...COLUMN_FIELDS, "date_format"].map((field) => MAPPING_PREFIX + field),
];

function uploadErrorsFrom(error) {
  const { fieldErrors, formError } = formErrorsFrom(error, FIELDS);
  const renamed = {};
  for (const [field, message] of Object.entries(fieldErrors)) {
    renamed[field.startsWith(MAPPING_PREFIX) ? field.slice(MAPPING_PREFIX.length) : field] =
      message;
  }
  return { fieldErrors: renamed, formError };
}

const CSV_READING = "Still reading the file's columns. Try again in a moment.";

export function UploadForm({ accounts, submitLabel = "Upload statement" }) {
  const createUpload = useCreateUpload();
  // The account the user picked. What the form USES is `selectedAccountId`:
  // the pick if it is still one of `accounts`, otherwise the first account.
  // `accounts` can change under the form (an account deleted in another
  // tab, refetched when this tab regains focus); the drop-down then shows
  // the first remaining account, and the upload must go to that same one,
  // not to the deleted id still in state (review of PR 33).
  const [accountId, setAccountId] = useState(accounts[0]?.id ?? "");
  const selectedAccountId = accounts.some((account) => account.id === accountId)
    ? accountId
    : (accounts[0]?.id ?? "");
  const [file, setFile] = useState(null);
  // The statement's password lives here only while the form is open, and
  // is cleared after every attempt; it is never stored (CLAUDE.md §7.2).
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState(NO_ERRORS);
  // A short "what happened" line after a send: `{ uploadId, text, showWhile }`.
  // It is shown only while it is still TRUE of that upload in the cached
  // list ("is being imported" until the import ends, "is password-protected"
  // until it is unlocked or deleted), so it never goes stale on the page.
  const [notice, setNotice] = useState(null);
  const uploads = useUploads();
  const noticeUpload = uploads.data?.find((upload) => upload.id === notice?.uploadId);
  const noticeText = noticeUpload && notice.showWhile(noticeUpload.status) ? notice.text : null;
  // A chosen CSV's first rows (`{ rows }`) and the column mapping the
  // user is building for it; both null for any other file.
  const [csv, setCsv] = useState(null);
  const [mapping, setMapping] = useState(null);
  // Counts file choices. Reading a CSV takes a moment; if another file is
  // chosen meanwhile, the older read's result must not replace the newer.
  const fileChoice = useRef(0);
  // Changing the key re-creates the file input, which empties it.
  const [fileInputKey, setFileInputKey] = useState(0);
  // Blocks a second submit while one is on its way (see LoginForm).
  const inFlight = useRef(false);

  function handleFileChange(event) {
    const chosen = event.target.files?.[0] ?? null;
    setFile(chosen);
    setNotice(null);
    // Checked at once, so a file that is too big is refused before the
    // user even presses the button.
    const problem = chosen ? fileProblem(chosen) : null;
    setErrors(problem ? { fieldErrors: { file: problem }, formError: null } : NO_ERRORS);
    if (!isPdf(chosen)) {
      setPassword("");
    }
    setCsv(null);
    setMapping(null);
    const choice = ++fileChoice.current;
    if (chosen && !problem && isCsv(chosen)) {
      readCsv(chosen, choice);
    }
  }

  async function readCsv(chosen, choice) {
    try {
      const { rows, headerIndex } = await readCsvPreview(chosen);
      if (choice !== fileChoice.current) {
        return; // another file was chosen meanwhile
      }
      setCsv({ rows });
      setMapping(suggestDateFormat(guessMapping(columnLabels(rows[headerIndex]), headerIndex), rows));
    } catch (error) {
      if (choice !== fileChoice.current) {
        return;
      }
      const message =
        error instanceof CsvPreviewError ? error.message : "We couldn't read this file.";
      setErrors({ fieldErrors: { file: message }, formError: null });
    }
  }

  function resetFile() {
    setFile(null);
    setPassword("");
    setCsv(null);
    setMapping(null);
    fileChoice.current += 1;
    setFileInputKey((key) => key + 1);
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (inFlight.current) {
      return;
    }
    const problem = fileProblem(file);
    if (problem) {
      setErrors({ fieldErrors: { file: problem }, formError: null });
      return;
    }
    let columnMapping = null;
    if (isCsv(file)) {
      if (mapping === null) {
        // Still being read, or unreadable (its message is already shown).
        if (!errors.fieldErrors.file) {
          setErrors({ fieldErrors: { file: CSV_READING }, formError: null });
        }
        return;
      }
      const mappingErrors = mappingProblems(mapping, dateSamples(csv.rows, mapping));
      if (Object.keys(mappingErrors).length > 0) {
        setErrors({
          fieldErrors: mappingErrors,
          formError: "Check the column choices below.",
        });
        return;
      }
      columnMapping = toColumnMapping(mapping);
    }

    inFlight.current = true;
    setErrors(NO_ERRORS);
    setNotice(null);
    try {
      const upload = await createUpload.mutateAsync({
        file,
        accountId: selectedAccountId,
        password: isPdf(file) ? password : "",
        columnMapping,
      });
      setNotice({
        uploadId: upload.id,
        text: `${file.name} was uploaded and is being imported.`,
        showWhile: isUnfinished,
      });
      resetFile();
    } catch (error) {
      handleError(error);
    } finally {
      inFlight.current = false;
    }
  }

  function handleError(error) {
    if (error?.code === "PASSWORD_REQUIRED") {
      // Not a failure: the locked PDF IS stored, and its row in the list
      // asks for the password.
      setNotice({
        uploadId: error.details?.upload_id,
        text: `${file.name} is password-protected. Enter its password in your uploads below.`,
        showWhile: (status) => status === "needs_password",
      });
      resetFile();
      return;
    }
    if (error?.code === "INCORRECT_PASSWORD") {
      // Nothing was stored; keep the file so only the password is retyped.
      setPassword("");
      setErrors({ fieldErrors: { password: error.message }, formError: null });
      return;
    }
    setErrors(uploadErrorsFrom(error));
  }

  const accountOptions = accounts.map((account) => ({
    value: account.id,
    label: `${account.nickname} (•••• ${account.masked_number})`,
  }));

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <FormError message={errors.formError} />
      {noticeText ? (
        // role="status": announced by screen readers, politely.
        <p role="status" className="text-sm">
          {noticeText}
        </p>
      ) : null}
      <FormField
        label="Account"
        name="account_id"
        options={accountOptions}
        value={selectedAccountId}
        onChange={(event) => setAccountId(event.target.value)}
        error={errors.fieldErrors.account_id}
      />
      <FormField
        key={fileInputKey}
        label="Statement file"
        name="file"
        type="file"
        accept={ACCEPTED_FILE_TYPES}
        onChange={handleFileChange}
        hint={`An ICICI PDF or .xls statement, or a CSV export. Up to ${MAX_UPLOAD_MB} MB.`}
        error={errors.fieldErrors.file}
      />
      {isPdf(file) ? (
        <FormField
          label="PDF password (if it has one)"
          name="password"
          type="password"
          autoComplete={STATEMENT_PASSWORD_AUTOCOMPLETE}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          hint="Banks often lock statement PDFs. The password is used once to open the file and is never stored."
          error={errors.fieldErrors.password}
        />
      ) : null}
      {csv && mapping ? (
        <ColumnMappingFields
          rows={csv.rows}
          values={mapping}
          onChange={setMapping}
          errors={errors.fieldErrors}
        />
      ) : null}
      <Button type="submit" disabled={createUpload.isPending}>
        {createUpload.isPending ? "Uploading…" : submitLabel}
      </Button>
    </form>
  );
}
