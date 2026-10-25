import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { openDelivery } from "./workspace-navigation";

for (const layout of ["legacy", "project-folders"] as const) {
  test(`${layout} delivery defaults reset both engines and preserve chosen paths`, async ({ page }, info) => {
    const session = await (await page.request.get("/api/session")).json();
    async function call(name: string, data: object = {}) {
      const response = await page.request.post(`/api/operations/${name}`, {
        headers: { Authorization: `Bearer ${session.token}` },
        data,
      });
      expect(response.ok(), await response.text()).toBe(true);
      return response.json();
    }
    const { project } = await call("create_project", { name: `默认位置 ${layout}` });
    const scene = await call("create_scene", { project_id: project.id });
    const motion = await call("create_motion_scene", {
      project_id: project.id,
      engine: "remotion-sdr-v1",
    });
    const options = await call("get_delivery_export_options");
    const expected = layout === "legacy"
      ? options.default_directory
      : `${options.default_directory}/项目/${project.id}/交付`;
    // Supply each storage contract without migrating the shared test daemon:
    // other browser tests must keep their own workspace layout assumptions.
    await page.route("**/api/operations/get_project", async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      await route.fulfill({
        response,
        json: {
          ...data,
          storage: layout === "legacy" ? null : { default_delivery_directory: expected },
        },
      });
    });
    await page.goto("/");
    await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
    for (const [work, field] of [
      [`wgsl:${scene.record.id}`, "#delivery-directory"],
      [`motion:${motion.record.id}`, "#motion-directory"],
    ]) {
      await page.getByLabel("当前作品", { exact: true }).selectOption(work);
      await openDelivery(page);
      const directory = page.locator(field);
      await expect(directory).toHaveValue(expected);
      const chosen = `${options.default_directory}/chosen-${layout}`;
      await directory.fill(chosen);
      await page.getByRole("button", { name: "3. 声音", exact: true }).click();
      await openDelivery(page);
      await expect(directory).toHaveValue(chosen);
      const reset = page.getByRole("button", { name: "使用默认位置", exact: true });
      await reset.focus();
      await page.keyboard.press("Enter");
      await expect(directory).toHaveValue(expected);
    }
    await page.screenshot({ path: info.outputPath("defaults-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    const scan = await new AxeBuilder({ page })
      .include(".delivery-destination:not([hidden])")
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    expect(scan.violations).toEqual([]);
    await page.screenshot({ path: info.outputPath("defaults-mobile.png"), fullPage: true });
    expect((await call("list_jobs", { project_id: project.id })).total).toBe(0);
  });
}
