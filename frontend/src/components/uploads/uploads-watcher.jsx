"use client";

// Polls every unfinished upload, on every signed-in page, and renders
// nothing. It lives in the layout so it keeps running while the user moves
// between pages: an import that finishes while they are on another page
// still updates the list and refreshes the other pages' data (see
// useUnfinishedUploadsPolling in src/lib/uploads.js).
//
// A component, not just a hook, because the layout is a server component
// and hooks only run in client components.

import { useUnfinishedUploadsPolling } from "@/lib/uploads";

export function UploadsWatcher() {
  useUnfinishedUploadsPolling();
  return null;
}
