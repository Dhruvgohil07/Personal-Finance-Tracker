// Vitest: the frontend's test runner (`npm test`).
//
// - plugin-react compiles JSX in the tests and components.
// - jsdom is a browser simulated in Node.js (document, window, fetch's
//   Headers...), so components can be rendered without a real browser.
// - "@" points at src/, the same import alias as jsconfig.json.

import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.js"],
    include: ["src/**/*.test.{js,jsx}"],
  },
});
