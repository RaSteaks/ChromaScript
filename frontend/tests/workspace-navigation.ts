// Five-stage navigation includes Sound before delivery.
import type { Page } from "@playwright/test";

/** Examples remain optional and collapsed in the shared library for every engine. */
export async function openExamples(page: Page) {
  const disclosure = page.locator(".workspace-library details");
  if ((await disclosure.getAttribute("open")) === null)
    await disclosure.locator("summary").click();
}

/** Navigate through the same common stage controls used by every source adapter. */
export async function openDelivery(page: Page) {
  await page.getByRole("button", { name: "4. 设置交付", exact: true }).click();
}
export async function openPreview(page: Page) {
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
}
