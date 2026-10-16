import { defineConfig } from "@playwright/test";
// Production UI and API are intentionally exercised together on the local daemon.
export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 45000,
  use: {
    baseURL: process.env.CHROMASCRIPT_URL || "http://127.0.0.1:8765",
    // Use WebKit for browser checks; do not launch Google Chrome.
    browserName: "webkit",
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  reporter: [["list"], ["html", { open: "never" }]],
});
