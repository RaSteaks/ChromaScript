import { defineConfig } from "@playwright/test";
// Tests must name an isolated daemon; never default to the user's live workspace.
const testURL = process.env.CHROMASCRIPT_URL;
if (!testURL) throw new Error("Set CHROMASCRIPT_URL to an isolated test service (for example http://127.0.0.1:8876). Tests never default to port 8765.");
export default defineConfig({
  testDir: "./tests",
  globalSetup: "./test-safety.mjs",
  workers: 1,
  timeout: 45000,
  use: {
    baseURL: testURL,
    // Use WebKit for browser checks; do not launch Google Chrome.
    browserName: "webkit",
    headless: true,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  reporter: [["list"], ["html", { open: "never" }]],
});
