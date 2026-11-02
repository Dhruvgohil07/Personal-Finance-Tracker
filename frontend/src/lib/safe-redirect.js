// Where to send someone after they log in: the page they wanted (?next=),
// but only if it is a page of THIS site.
//
// Without this check, a link like /login?next=https://evil.example would
// send a user to another site straight after a real login, where a fake
// "session expired, log in again" page could collect their password. This
// is called an "open redirect".

export const HOME_PATH = "/";

// Pages it makes no sense to return to after logging in.
const AUTH_PAGES = new Set(["/login", "/register"]);

// Any origin that can't be real; only used to resolve the path against.
const PLACEHOLDER_ORIGIN = "http://kharcha.invalid";

/** The `next` value if it is a path on this site, otherwise the home page. */
export function safeNextPath(next) {
  // A path must start with exactly one "/". "//evil.example" is a
  // "protocol-relative" URL (another site), and browsers treat "\" like "/",
  // so "/\evil.example" is one too.
  if (typeof next !== "string" || !next.startsWith("/") || /^\/[/\\]/.test(next)) {
    return HOME_PATH;
  }

  // Let the browser's own URL parser decide where the value really points;
  // anything that leaves the placeholder origin is refused.
  let url;
  try {
    url = new URL(next, PLACEHOLDER_ORIGIN);
  } catch {
    return HOME_PATH;
  }
  if (url.origin !== PLACEHOLDER_ORIGIN || AUTH_PAGES.has(url.pathname)) {
    return HOME_PATH;
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

/** The login page's URL that returns to `path` afterwards. */
export function loginPathFor(path) {
  if (!path || path === HOME_PATH) {
    return "/login";
  }
  return `/login?next=${encodeURIComponent(path)}`;
}
