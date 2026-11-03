"use client";

// The "upload a statement" form, used on the uploads page and as the
// first-run step on the home page.
//
// The file input is UNCONTROLLED: a browser never lets a page set a file
// input's value (that would let a page pick files off your disk), so React
// can't hold it in state the way it holds a text field. The chosen File is
// copied into state from onChange, and to empty the input after an upload
// the input is re-created by changing its `key`.

import { useRef, useState } from "react";

import { Button } from "@/components/button";
import { FormError } from "@/components/form-error";
import { FormField } from "@/components/form-field";
import { formErrorsFrom } from "@/lib/form-errors";
import {
  ACCEPTED_FILE_TYPES,
  MAX_UPLOAD_MB,
  fileProblem,
  isPdf,
  isUnfinished,
  useCreateUpload,
  useUploads,
} from "@/lib/uploads";

const FIELDS = ["file", "account_id", "password"];
const NO_ERRORS = { fieldErrors: {}, formError: null };

export function UploadForm({ accounts, submitLabel = "Upload statement" }) {
  const createUpload = useCreateUpload();
  const [accountId, setAccountId] = useState(accounts[0]?.id ?? "");
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
  }

  function resetFile() {
    setFile(null);
    setPassword("");
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

    inFlight.current = true;
    setErrors(NO_ERRORS);
    setNotice(null);
    try {
      const upload = await createUpload.mutateAsync({
        file,
        accountId,
        password: isPdf(file) ? password : "",
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
    setErrors(formErrorsFrom(error, FIELDS));
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
        value={accountId}
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
          autoComplete="off"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          hint="Banks often lock statement PDFs. The password is used once to open the file and is never stored."
          error={errors.fieldErrors.password}
        />
      ) : null}
      <Button type="submit" disabled={createUpload.isPending}>
        {createUpload.isPending ? "Uploading…" : submitLabel}
      </Button>
    </form>
  );
}
