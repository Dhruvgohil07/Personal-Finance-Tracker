// Next.js Proxy (called "Middleware" before Next.js 16): runs on the server
// before a request is handled, here only for API calls (see `config`).
//
// It adds the signed client-IP headers (src/lib/client-ip.js) to the request
// that next.config.mjs then forwards to the backend.

import { NextResponse } from "next/server";

import { headersForBackend } from "@/lib/client-ip";

export async function proxy(request) {
  const headers = await headersForBackend(request.headers, process.env.PROXY_SHARED_SECRET);
  // `request: { headers }` changes the headers sent UPSTREAM (to the
  // rewrite's destination). Plain `headers:` would set them on the response
  // to the browser instead, which is not what we want.
  return NextResponse.next({ request: { headers } });
}

export const config = {
  // Only API calls: pages and static files don't need it.
  matcher: "/api/v1/:path*",
};
