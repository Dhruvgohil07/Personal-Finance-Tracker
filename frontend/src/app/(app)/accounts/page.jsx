// A server component, so it can set the page's title (`metadata` can't be
// exported from a "use client" file); the interactive part is AccountsView.

import { AccountsView } from "@/components/accounts/accounts-view";

export const metadata = { title: "Accounts · Kharcha" };

export default function AccountsPage() {
  return <AccountsView />;
}
