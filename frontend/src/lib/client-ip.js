// The browser's IP, signed for the backend (ADR 007, Phase 3 Step 2).
//
// Every API call reaches the backend from this Next.js server, so without
// help the backend would see one IP for all visitors and put everyone on one
// rate-limit counter. So the proxy (src/proxy.js) tells it the browser's IP,
// plus an HMAC-SHA256 of that IP made with PROXY_SHARED_SECRET. The backend
// trusts the IP only when it can recompute the same HMAC; the secret itself
// never travels.
//
// Pure functions, no Next.js imports, so they can be unit tested on their own.

// Must match CLIENT_IP_HEADER / CLIENT_IP_SIGNATURE_HEADER in
// backend/app/core/rate_limit.py.
export const CLIENT_IP_HEADER = "x-kharcha-client-ip";
export const CLIENT_IP_SIGNATURE_HEADER = "x-kharcha-client-ip-signature";

/**
 * HMAC-SHA256 of `ip` with `secret`, as lowercase hex: exactly what the
 * backend's `sign_client_ip()` computes.
 *
 * Uses the Web Crypto API (`crypto.subtle`), built into Node.js and every
 * browser, so no package is needed. It is asynchronous: it returns a Promise.
 */
export async function signClientIp(ip, secret) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false, // the key can't be exported again
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(ip));
  // ArrayBuffer -> "9d72ea7f...": each byte as two hex digits.
  return Array.from(new Uint8Array(signature), (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

/**
 * The browser's IP from the incoming request's headers, or null if unknown.
 *
 * `x-forwarded-for` is "client, proxy1, proxy2"; the first entry is the
 * browser. This is only trustworthy because Vercel OVERWRITES the header with
 * the address it actually received the connection from, so a browser can't
 * choose it. On a host that only appends to it, a client could put any IP
 * first (ADR 022). Locally there is no proxy in front and no header: the
 * function returns null and the backend uses the connection's address.
 */
export function clientIpFrom(headers) {
  const forwardedFor = headers.get("x-forwarded-for");
  if (!forwardedFor) {
    return null;
  }
  const first = forwardedFor.split(",")[0].trim();
  return first || null;
}

/**
 * The request headers to send to the backend.
 *
 * Copies the incoming headers, always REMOVES any client-IP headers the
 * browser sent (only this server may set them), then adds the IP and its
 * signature when both the secret and the IP are known.
 */
export async function headersForBackend(incoming, secret) {
  const headers = new Headers(incoming);
  headers.delete(CLIENT_IP_HEADER);
  headers.delete(CLIENT_IP_SIGNATURE_HEADER);

  const ip = clientIpFrom(incoming);
  if (secret && ip) {
    headers.set(CLIENT_IP_HEADER, ip);
    headers.set(CLIENT_IP_SIGNATURE_HEADER, await signClientIp(ip, secret));
  }
  return headers;
}
