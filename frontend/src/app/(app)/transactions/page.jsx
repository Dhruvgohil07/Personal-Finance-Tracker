// A server component, so it can set the page's title (see the accounts
// page); the interactive part is TransactionsView.
//
// Suspense: TransactionsView reads the URL's query with useSearchParams,
// which Next.js only knows in the browser. Without a Suspense boundary
// around it, `next build` refuses the page (the static HTML would have to
// wait for a query string it can't have); with one, the fallback is the
// static part and the view fills in once the browser runs.

import { Suspense } from "react";

import { TransactionsView } from "@/components/transactions/transactions-view";

export const metadata = { title: "Transactions · Kharcha" };

export default function TransactionsPage() {
  return (
    <Suspense fallback={<p role="status">Loading transactions…</p>}>
      <TransactionsView />
    </Suspense>
  );
}
