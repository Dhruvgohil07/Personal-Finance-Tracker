// Runs before every test file.

// Adds DOM matchers to expect(), e.g. expect(element).toHaveTextContent("…").
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Unmount whatever a test rendered, so the next test starts with an empty page.
afterEach(() => {
  cleanup();
});
