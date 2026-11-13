import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

test("fleet list, device command, and OTA update", async ({ page }) => {
  await page.goto("/devices");

  const fleet = page.getByTestId("fleet-table");
  await expect(fleet).toBeVisible({ timeout: 20_000 });
  const row = page.getByTestId("device-row-vac-001");
  await expect(row).toBeVisible();
  await expect(row).toContainText("vac-001");
  await expect(row).toContainText("online");

  await row.getByRole("link", { name: "vac-001" }).click();
  await expect(page).toHaveURL(/\/devices\/vac-001$/);
  await expect(page.getByTestId("device-title")).toHaveText("vac-001");

  // Force a state flip through the API command path, then start again.
  await page.getByRole("button", { name: "Dock", exact: true }).click();
  await expect(page.getByTestId("reported-state")).toHaveText("DOCKED", { timeout: 15_000 });

  await page.getByRole("button", { name: "Start", exact: true }).click();
  await expect(page.getByTestId("reported-state")).toHaveText("RUNNING", { timeout: 15_000 });

  await page.getByRole("button", { name: "Install v1.3.0" }).click();
  await expect(page.getByTestId("fw-version")).toHaveText("1.3.0", { timeout: 30_000 });
  await expect(page.getByTestId("ota-status")).toContainText("success", { timeout: 30_000 });
});

test("analytics page renders fleet charts and alerts", async ({ page }) => {
  await page.goto("/analytics");
  await expect(page.getByTestId("analytics-page")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("usage-histogram")).toBeVisible();
  await expect(page.getByTestId("error-pareto")).toBeVisible();
  await expect(page.getByTestId("alert-feed")).toBeVisible();
});
