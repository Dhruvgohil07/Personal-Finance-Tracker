// A list of uploads, each with its account's nickname. Shared by the
// uploads page (all of them) and the home page (just the latest).

import { UploadItem } from "@/components/uploads/upload-item";

export function UploadList({ uploads, accounts }) {
  // Look-up table from account id to nickname, built once per render
  // instead of searching the accounts array for every upload.
  const nicknames = new Map(accounts.map((account) => [account.id, account.nickname]));

  return (
    <ul className="flex flex-col gap-3">
      {uploads.map((upload) => (
        <UploadItem
          key={upload.id}
          upload={upload}
          accountName={nicknames.get(upload.account_id) ?? "An account"}
        />
      ))}
    </ul>
  );
}
