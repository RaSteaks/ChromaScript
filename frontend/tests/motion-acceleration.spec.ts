import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { fileURLToPath } from "node:url";
import { openDelivery } from "./workspace-navigation";

test("GPU/encoder choices keep drafts, refresh budgets and survive failure; accessibility", async ({
  page,
}) => {
  const session = await (await page.request.get("/api/session")).json();
  async function call(name: string, data: unknown) {
    const response = await page.request.post(`/api/operations/${name}`, {
      headers: { Authorization: `Bearer ${session.token}` },
      data,
    });
    expect(response.ok(), await response.text()).toBeTruthy();
    return response.json();
  }
  const { project } = await call("create_project", {
    name: `GPU 交付 ${Date.now()}`,
  });
  const scene = await call("create_motion_scene", { project_id: project.id });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("当前作品", { exact: true })
    .selectOption(`motion:${scene.record.id}`);
  await openDelivery(page);
  await page.getByLabel("导出模式", { exact: true }).selectOption("video-only");
  await page.getByText("视频加速与编码", { exact: true }).click();
  const processing = page.getByLabel("视频处理", { exact: true });
  const encoder = page.getByLabel("视频编码", { exact: true });
  const quality = page.getByLabel("编码优先", { exact: true });
  await expect(processing).toHaveValue("auto");
  await expect(encoder).toHaveValue("auto");
  await expect(quality).toHaveValue("quality");
  await expect(
    page.getByRole("region", { name: "渲染磁盘空间预估" }),
  ).toContainText("不写逐帧临时文件");
  await processing.focus();
  await processing.press("Space");
  await processing.press("Escape");
  await expect(processing).toBeFocused();
  const estimate = page.waitForRequest(
    (request) =>
      request.url().endsWith("/estimate_motion_render") &&
      request.postDataJSON().output.video_processing === "reference",
  );
  await processing.selectOption("reference");
  await estimate;
  await expect(
    page.getByRole("region", { name: "渲染磁盘空间预估" }),
  ).toContainText("每路最多保留一帧");
  await encoder.selectOption("software");
  await quality.selectOption("balanced");
  await page.getByRole("button", { name: "刷新作品列表", exact: true }).click();
  await expect(processing).toHaveValue("reference");
  await expect(encoder).toHaveValue("software");
  await expect(quality).toHaveValue("balanced");
  // Intercept submissions: no production Chrome process may be started by this UI test.
  await page.route("**/api/operations/submit_motion_render", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "测试硬件不可用" } },
    }),
  );
  const submitted = page.waitForRequest((request) =>
    request.url().endsWith("/submit_motion_render"),
  );
  await page.getByRole("button", { name: "导出动画", exact: true }).click();
  const output = (await submitted).postDataJSON().output;
  expect(output.video_processing).toBe("reference");
  expect(output.video_encoder).toBe("software");
  expect(output.video_quality).toBe("balanced");
  await expect(
    page.getByRole("alert").filter({ hasText: "测试硬件不可用" }),
  ).toBeVisible();
  await expect(processing).toHaveValue("reference");
  expect((await call("list_jobs", { project_id: project.id })).total).toBe(0);
  await page
    .locator(".motion-export")
    .screenshot({
      path: fileURLToPath(
        new URL("../../reports/gpu-video-desktop.png", import.meta.url),
      ),
    });
  await page.setViewportSize({ width: 390, height: 844 });
  await processing.scrollIntoViewIfNeeded();
  await expect(processing).toBeVisible();
  expect(
    (await new AxeBuilder({ page }).include(".motion-export").analyze())
      .violations,
  ).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  await page
    .locator(".motion-export")
    .screenshot({
      path: fileURLToPath(
        new URL("../../reports/gpu-video-mobile.png", import.meta.url),
      ),
    });
});
