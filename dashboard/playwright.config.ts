import fs from "node:fs";
import { defineConfig } from "@playwright/test";

// Prefer the bundled Chromium (CI installs it). On a developer Mac without a
// downloaded Playwright browser, fall back to the installed Google Chrome.
const chromeInstalled = fs.existsSync("/Applications/Google Chrome.app");
const localChannel =
  process.env.PLAYWRIGHT_CHANNEL ?? (chromeInstalled ? "chrome" : undefined);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  globalSetup: "./e2e/global-setup.ts",
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:4173",
    ...(localChannel ? { channel: localChannel } : {}),
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
});
