// Named "Middleware" in 16.4.3; the bundled docs already call it
// unstable_doesProxyMatch, so the name may change when Next.js is upgraded.
import { unstable_doesMiddlewareMatch as doesProxyMatch } from "next/experimental/testing/server";
import { describe, expect, it } from "vitest";

import nextConfig from "../next.config.mjs";

import { config } from "./proxy";

// Which requests the proxy runs for. The helper is Next.js's own way of
// testing `matcher`.
const runsFor = (url) => doesProxyMatch({ config, nextConfig, url });

describe("the proxy's matcher", () => {
  it.each([
    "/api/v1/auth/login", // rate limited per IP: needs the signed IP
    "/api/v1/auth/refresh",
    "/api/v1/me", // the audit log records the IP
    "/api/v1/accounts",
    "/api/v1/uploads",
  ])("runs for the API call %s", (url) => {
    expect(runsFor(url)).toBe(true);
  });

  it.each(["/", "/login", "/_next/static/chunks/main.js", "/api/v2/accounts"])(
    "does not run for %s, which is not an API call",
    (url) => {
      expect(runsFor(url)).toBe(false);
    },
  );
});

describe("the request body limit", () => {
  it("leaves room for a 10 MB statement plus its multipart framing", () => {
    // Next.js cuts every body off at this size while a proxy exists. The
    // backend's MAX_UPLOAD_MB is 10, so anything at or under that limit
    // must arrive whole, and the backend's own 413 answers the rest.
    const limitMb = Number.parseInt(nextConfig.experimental.proxyClientMaxBodySize, 10);

    expect(limitMb).toBeGreaterThan(10);
  });
});
