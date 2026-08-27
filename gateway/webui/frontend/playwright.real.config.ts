import { existsSync } from "node:fs";

import { defineConfig } from "@playwright/test";

const cachedChromium = "/home/test/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell";
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
  ?? (existsSync(cachedChromium) ? cachedChromium : undefined);

export default defineConfig({
  testDir: "./tests/e2e-real",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: process.env.JKAGENT_REAL_BASE_URL ?? "http://127.0.0.1:19120/ui/",
    trace: "retain-on-failure",
    launchOptions: executablePath ? { executablePath } : undefined,
  },
});
