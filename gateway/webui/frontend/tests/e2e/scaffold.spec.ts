import { expect, test } from "@playwright/test";

test("serves the React scaffold under the gateway /ui base path", async ({ page }) => {
  await page.goto("#/chat");
  await expect(page).toHaveTitle("JKagent 控制台");
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await expect(page.locator('a[href="#/chat"]')).toBeVisible();
});
