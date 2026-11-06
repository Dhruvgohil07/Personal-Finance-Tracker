import { describe, expect, it } from "vitest";

import { plural } from "@/lib/text";

describe("plural", () => {
  it("is singular for exactly one", () => {
    expect(plural(1, "transaction")).toBe("1 transaction");
    expect(plural(0, "transaction")).toBe("0 transactions");
    expect(plural(3, "minute")).toBe("3 minutes");
  });
});
