import { existsSync } from "node:fs";

import { defineConfig } from "@playwright/test";

const cachedChromium = "/home/test/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell";
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
  ?? (existsSync(cachedChromium) ? cachedChromium : undefined);

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:4173/ui/",
    trace: "retain-on-failure",
    launchOptions: executablePath ? { executablePath } : undefined,
  },
  webServer: {
    command: "npm run build && npm run preview",
    url: "http://127.0.0.1:4173/ui/",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
