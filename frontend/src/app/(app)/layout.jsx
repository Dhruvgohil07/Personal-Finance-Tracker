// The layout of every page for signed-in users. "(app)" is a route group:
// it doesn't appear in the URL, so (app)/page.jsx is the home page "/".
//
// RequireAuth shows these pages only with a session, and otherwise sends
// the visitor to /login?next=<this page>.

import { AppHeader } from "@/components/app-header";
import { RequireAuth } from "@/components/require-auth";
import { UploadsWatcher } from "@/components/uploads/uploads-watcher";

export default function AppLayout({ children }) {
  return (
    <RequireAuth>
      {/* Polls unfinished uploads whichever page is open (renders nothing). */}
      <UploadsWatcher />
      <AppHeader />
      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-8">{children}</main>
    </RequireAuth>
  );
}
