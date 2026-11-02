// Next.js configuration (ADR 022).
//
// The browser only ever talks to THIS app's origin (Phase 3 plan, hard
// constraint 2). Every request to /api/v1/... is forwarded, server side, to
// the FastAPI backend at API_ORIGIN. So the refresh-token cookie never has to
// cross sites (it stays SameSite=Strict, ADR 005) and no CORS is needed.

// Where the backend runs. Locally: uvicorn on port 8000. 127.0.0.1, not
// "localhost": on Windows "localhost" tries IPv6 first (see .env.example).
const DEFAULT_API_ORIGIN = "http://127.0.0.1:8000";

function apiOrigin() {
  const origin = process.env.API_ORIGIN;
  // VERCEL=1 is set on Vercel's build machines. A deployment without
  // API_ORIGIN would quietly forward every API call to localhost there,
  // so fail the build instead.
  if (!origin && process.env.VERCEL) {
    throw new Error("API_ORIGIN must be set for a Vercel build (the backend's URL)");
  }
  return (origin ?? DEFAULT_API_ORIGIN).replace(/\/$/, "");
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Next.js 16.4's defaults for new apps (create-next-app). Data fetching is
  // dynamic unless a component opts into caching with "use cache"; both
  // flags become always-on in the next major version.
  cacheComponents: true,
  partialPrefetching: true,
  experimental: {
    // Because src/proxy.js exists, Next.js copies every request body into
    // memory and CUTS IT OFF at this size (default 10 MB); a larger body
    // fails with a 500 before reaching the backend. A statement upload may
    // be 10 MB (the backend's MAX_UPLOAD_MB) plus the multipart framing, so
    // the limit sits a little above it: the backend's own "file too large"
    // (413) then answers anything bigger. Raise both together (review of
    // PR 30, checked live with an 11 MB body).
    proxyClientMaxBodySize: "12mb",
  },
  // Tailwind CSS 4 runs as a Turbopack loader on every .css file.
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  // A rewrite is invisible to the browser: it asks /api/v1/accounts, and
  // the Next.js server fetches API_ORIGIN/api/v1/accounts and passes the
  // answer (including Set-Cookie) back. src/proxy.js runs first and adds
  // the signed client-IP headers.
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${apiOrigin()}/api/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
