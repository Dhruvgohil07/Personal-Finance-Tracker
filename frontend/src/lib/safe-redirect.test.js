import { describe, expect, it } from "vitest";

import { loginPathFor, safeNextPath } from "@/lib/safe-redirect";

describe("safeNextPath", () => {
  it.each([
    ["/accounts", "/accounts"],
    ["/transactions?month=2026-10", "/transactions?month=2026-10"],
    ["/uploads/u1#status", "/uploads/u1#status"],
    ["/a/../accounts", "/accounts"], // normalized by the URL parser
  ])("keeps the path %s on this site", (next, expected) => {
    expect(safeNextPath(next)).toBe(expected);
  });

  it.each([
    ["https://evil.example/login"], // another site
    ["//evil.example"], // protocol-relative: another site
    ["/\\evil.example"], // browsers read "\" as "/"
    ["\\/evil.example"],
    ["javascript:alert(1)"],
    ["evil.example"], // not a path
    [""],
    [null],
    [undefined],
  ])("refuses %j and goes home instead", (next) => {
    expect(safeNextPath(next)).toBe("/");
  });

  it.each([["/login"], ["/register"], ["/login?next=/accounts"]])(
    "doesn't return to %s, the login pages themselves",
    (next) => {
      expect(safeNextPath(next)).toBe("/");
    },
  );
});

describe("loginPathFor", () => {
  it("remembers the page to come back to", () => {
    expect(loginPathFor("/transactions")).toBe("/login?next=%2Ftransactions");
  });

  it("needs no ?next= for the home page", () => {
    expect(loginPathFor("/")).toBe("/login");
    expect(loginPathFor(null)).toBe("/login");
  });
});
