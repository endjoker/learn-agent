import { expect, test } from "@playwright/test";

const routes = [
  "chat", "plan", "goal", "mcp", "skills", "prompt", "status", "cron", "workspace", "agent-editor", "settings",
] as const;

test("real gateway serves all 11 migrated routes", async ({ page }) => {
  for (const hash of routes) {
    await page.goto(`#/${hash}`);
    await expect(page.locator(`#main[data-route="${hash}"]`)).toBeAttached({ timeout: 10_000 });
  }
});
