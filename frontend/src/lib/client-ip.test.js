import { describe, expect, it } from "vitest";

import {
  CLIENT_IP_HEADER,
  CLIENT_IP_SIGNATURE_HEADER,
  clientIpFrom,
  headersForBackend,
  signClientIp,
} from "@/lib/client-ip";

// The test vector from backend/tests/unit/test_rate_limit_keys.py, checked
// there with OpenSSL. If this matches, the backend accepts our signature.
const SECRET = "kharcha-test-proxy-secret-0123456789abcdef";
const IP = "198.51.100.23";
const SIGNATURE = "9d72ea7f55c57296072946f85fbc5b8c831ab42c447ef86ce6c4fc4c08e97ce8";

describe("signClientIp", () => {
  it("produces exactly the backend's HMAC-SHA256 in lowercase hex", async () => {
    expect(await signClientIp(IP, SECRET)).toBe(SIGNATURE);
  });
});

describe("clientIpFrom", () => {
  it("takes the first entry of x-forwarded-for: the browser", () => {
    const headers = new Headers({ "x-forwarded-for": "198.51.100.23, 10.0.0.1, 10.0.0.2" });
    expect(clientIpFrom(headers)).toBe("198.51.100.23");
  });

  it.each([[""], [" , 10.0.0.1"]])("is null when the first entry is empty (%j)", (value) => {
    expect(clientIpFrom(new Headers({ "x-forwarded-for": value }))).toBeNull();
  });

  it("is null without the header (local development)", () => {
    expect(clientIpFrom(new Headers())).toBeNull();
  });
});

describe("headersForBackend", () => {
  it("adds the IP and its signature", async () => {
    const incoming = new Headers({ "x-forwarded-for": IP, cookie: "refresh_token=abc" });

    const headers = await headersForBackend(incoming, SECRET);

    expect(headers.get(CLIENT_IP_HEADER)).toBe(IP);
    expect(headers.get(CLIENT_IP_SIGNATURE_HEADER)).toBe(SIGNATURE);
    // Everything else is passed on unchanged.
    expect(headers.get("cookie")).toBe("refresh_token=abc");
  });

  it("removes client-IP headers the browser made up", async () => {
    const incoming = new Headers({
      [CLIENT_IP_HEADER]: "203.0.113.99",
      [CLIENT_IP_SIGNATURE_HEADER]: "forged",
    });

    // No secret and no forwarded IP: nothing may be passed on at all.
    const headers = await headersForBackend(incoming, undefined);

    expect(headers.has(CLIENT_IP_HEADER)).toBe(false);
    expect(headers.has(CLIENT_IP_SIGNATURE_HEADER)).toBe(false);
  });

  it("replaces made-up headers with the real, signed ones", async () => {
    const incoming = new Headers({
      "x-forwarded-for": IP,
      [CLIENT_IP_HEADER]: "203.0.113.99",
      [CLIENT_IP_SIGNATURE_HEADER]: "forged",
    });

    const headers = await headersForBackend(incoming, SECRET);

    expect(headers.get(CLIENT_IP_HEADER)).toBe(IP);
    expect(headers.get(CLIENT_IP_SIGNATURE_HEADER)).toBe(SIGNATURE);
  });

  it("adds nothing without a secret (local development)", async () => {
    const headers = await headersForBackend(new Headers({ "x-forwarded-for": IP }), "");

    expect(headers.has(CLIENT_IP_HEADER)).toBe(false);
  });

  it("does not change the incoming headers", async () => {
    const incoming = new Headers({ "x-forwarded-for": IP });

    await headersForBackend(incoming, SECRET);

    expect(incoming.has(CLIENT_IP_HEADER)).toBe(false);
  });
});
