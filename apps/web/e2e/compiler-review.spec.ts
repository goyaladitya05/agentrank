import { expect, test } from "@playwright/test";

const key = process.env.AGENTRANK_E2E_KEY;

test("merchant reviews facts and publishes one immutable representation", async ({ page }) => {
  if (key === undefined) throw new Error("AGENTRANK_E2E_KEY is required");
  await page.goto("/login");
  await page.getByLabel("Merchant API key").fill(key);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("link", { name: "Compiler" }).click();
  await expect(page.getByText(/semantic fact\(s\) need review/)).toBeVisible();
  await page.getByRole("link", { name: "voltedge-merchant-source@1" }).click();
  const forms = page.locator('form:has(button[value="correct"])');
  await expect(forms).toHaveCount(2);
  for (let index = 0; index < 2; index += 1) {
    const form = forms.nth(0);
    await form.getByLabel("Corrected value").fill("65");
    await form.getByLabel("Source field").fill("products[VE-CHG-100].description");
    await form.getByLabel("Source excerpt").fill("65W");
    await form.getByRole("button", { name: "Confirm correction" }).click();
    await page.waitForLoadState("networkidle");
  }
  await page.getByRole("button", { name: "Review publication" }).click();
  await expect(page.getByText("This does not rerun a benchmark.")).toBeVisible();
  await page.getByRole("button", { name: "Publish representation" }).click();
  await expect(page.getByText("Agent-ready representation published:")).toBeVisible();
});
