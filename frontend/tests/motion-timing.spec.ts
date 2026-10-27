import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { fileURLToPath } from "node:url";
import { openDelivery, openPreview } from "./workspace-navigation";

test("output timing preserves source, previews the output recipe, retains errors and supports accessibility", async ({
  page,
}) => {
  test.setTimeout(90000);
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
    name: `输出帧率 ${Date.now()}`,
  });
  const created = await call("create_motion_scene", {
    project_id: project.id,
    template_id: "radiance",
  });
  const saved = await call("update_motion_scene", {
    scene_id: created.record.id,
    expected_revision_id: created.revision_id,
    spec: {
      ...created.scene,
      width: 64,
      height: 36,
      frames: 600,
      fps: { numerator: 60, denominator: 1 },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("当前作品", { exact: true })
    .selectOption(`motion:${saved.record.id}`);
  await openDelivery(page);
  const review = page.getByRole("region", {
    name: "输出时间与预览",
    exact: true,
  });
  await page.getByLabel("正式导出采样", { exact: true }).selectOption("1");
  await review.getByLabel("输出帧率", { exact: true }).selectOption("30/1");
  await expect(review).toContainText("输出 300 帧 · 30/1 fps · 10.0000 秒");
  await review.getByLabel("预览起点（秒）", { exact: true }).fill("2");
  await review.getByLabel("预览帧数", { exact: true }).selectOption("1");
  const request = page.waitForRequest((r) =>
    r.url().endsWith("/render_motion_output_preview"),
  );
  await review
    .getByRole("button", { name: "生成输出预览", exact: true })
    .click();
  const args = (await request).postDataJSON();
  expect(args.spec.fps.numerator).toBe(60);
  expect(args.output.fps.numerator).toBe(30);
  expect(args.start_frame).toBe(60);
  await expect(review.getByRole("img", { name: "输出第 60 帧" })).toBeVisible({
    timeout: 30000,
  });
  await review.getByLabel("时间采样", { exact: true }).selectOption("4");
  await expect(review).toContainText("当前画面为旧预览");
  await expect(
    review.getByRole("button", { name: "播放实时预览", exact: true }),
  ).toBeDisabled();
  await review.getByLabel("输出帧率", { exact: true }).focus();
  await review.getByLabel("输出帧率", { exact: true }).press("Space");
  await review.getByLabel("输出帧率", { exact: true }).press("Escape");
  await expect(review.getByLabel("输出帧率", { exact: true })).toBeFocused();
  await review.getByLabel("预览帧数", { exact: true }).selectOption("15");
  await review
    .getByRole("button", { name: "生成输出预览", exact: true })
    .click();
  await expect(
    review.getByRole("button", { name: "播放实时预览", exact: true }),
  ).toBeEnabled({ timeout: 30000 });
  await review
    .getByRole("button", { name: "播放实时预览", exact: true })
    .click();
  await expect(review.getByRole("img", { name: "输出第 74 帧" })).toBeVisible();
  await expect(
    review.getByRole("button", { name: "播放实时预览", exact: true }),
  ).toBeVisible();
  let release!: () => void;
  let fetched!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const arrived = new Promise<void>((resolve) => {
    fetched = resolve;
  });
  await page.route(
    "**/api/operations/render_motion_output_preview",
    async (route) => {
      const response = await route.fetch();
      fetched();
      await held;
      await route.fulfill({ response });
    },
  );
  await review
    .getByRole("button", { name: "生成输出预览", exact: true })
    .click();
  await arrived;
  await review.getByLabel("输出帧率", { exact: true }).selectOption("24/1");
  release();
  await expect(
    review.getByRole("button", { name: "生成输出预览", exact: true }),
  ).toBeEnabled();
  // The late 30fps response cannot replace the retained picture or certify 24fps.
  await expect(review.getByRole("img", { name: "输出第 74 帧" })).toBeVisible();
  await expect(review).toContainText("当前画面为旧预览");
  await page.unroute("**/api/operations/render_motion_output_preview");
  await page.route("**/api/operations/render_motion_output_preview", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "测试输出预览失败" } },
    }),
  );
  await review
    .getByRole("button", { name: "生成输出预览", exact: true })
    .click();
  await expect(review.getByRole("alert")).toContainText("测试输出预览失败");
  await expect(review.getByRole("img", { name: "输出第 74 帧" })).toBeVisible();
  await review.getByLabel("自定义输出帧率", { exact: true }).fill("30/0");
  await expect(
    page.getByRole("button", { name: "导出动画", exact: true }),
  ).toBeDisabled();
  await expect(
    review.getByLabel("自定义输出帧率", { exact: true }),
  ).toHaveValue("30/0");
  await review.getByLabel("自定义输出帧率", { exact: true }).fill("24/1");
  await expect(review).toContainText("输出 240 帧");
  await expect(
    review.getByLabel("预览起点（秒）", { exact: true }),
  ).toHaveValue("2");
  await review.getByLabel("自定义输出帧率", { exact: true }).fill("024/1");
  await expect(
    page.getByRole("button", { name: "导出动画", exact: true }),
  ).toBeEnabled();
  await page.route("**/api/operations/get_motion_time_plan", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "测试时间计划失败" } },
    }),
  );
  await review.getByLabel("输出帧率", { exact: true }).selectOption("25/1");
  await expect(review).toContainText("测试时间计划失败");
  await expect(
    page.getByRole("button", { name: "导出动画", exact: true }),
  ).toBeDisabled();
  await page.unroute("**/api/operations/get_motion_time_plan");
  await review
    .getByRole("button", { name: "重试时间计算", exact: true })
    .click();
  await expect(review).toContainText("输出 250 帧");
  await review.getByLabel("输出帧率", { exact: true }).selectOption("24/1");
  await openPreview(page);
  await expect(page.getByLabel("总帧数", { exact: true })).toHaveValue("600");
  await expect(page.getByLabel("常用帧率", { exact: true })).toHaveValue(
    "60/1",
  );
  await openDelivery(page);
  await expect(review.getByLabel("输出帧率", { exact: true })).toHaveValue(
    "24/1",
  );
  const original = await call("get_motion_scene", {
    scene_id: saved.record.id,
  });
  expect(original.revision_id).toBe(saved.revision_id);
  expect((await call("list_jobs", { project_id: project.id })).total).toBe(0);
  await expect(review).toContainText("输出 240 帧");
  await review.screenshot({
    path: fileURLToPath(
      new URL("../../reports/motion-timing-desktop.png", import.meta.url),
    ),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await review.scrollIntoViewIfNeeded();
  expect(
    (
      await new AxeBuilder({ page })
        .include('[aria-label="输出时间与预览"]')
        .analyze()
    ).violations,
  ).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  await review.screenshot({
    path: fileURLToPath(
      new URL("../../reports/motion-timing-mobile.png", import.meta.url),
    ),
  });
});
