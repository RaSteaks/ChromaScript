import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("ACEScct intermediate sampling, stale drafts and submission; accessibility", async ({
  page,
}) => {
  const session = await (await page.request.get("/api/session")).json();
  async function call(name: string, data: unknown) {
    const response = await page.request.post(`/api/operations/${name}`, {
      headers: { Authorization: `Bearer ${session.token}` },
      data,
    });
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  }
  const { project } = await call("create_project", {
    name: `中间片 ${Date.now()}`,
  });
  const blank = await call("create_motion_scene", { project_id: project.id });
  const saved = await call("update_motion_scene", {
    scene_id: blank.record.id,
    expected_revision_id: blank.revision_id,
    spec: { ...blank.scene, width: 64, height: 32, frames: 24 },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await page.getByLabel("动画项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("已保存的动画", { exact: true })
    .selectOption(saved.record.id);
  await page.getByLabel("导出模式", { exact: true }).selectOption("video-only");
  const video = page.getByLabel("视频输出", { exact: true });
  await expect(
    video.locator('option[value="prores-4444-acescct"]'),
  ).toBeEnabled();
  await video.selectOption("prores-4444-acescct");
  await expect(video).toHaveValue("prores-4444-acescct");
  await expect(video.locator("option")).not.toContainText(["4444 XQ"]);
  const panel = page.getByRole("region", { name: "渲染磁盘空间预估" });
  await expect(
    panel.getByText("预计磁盘峰值占用", { exact: true }),
  ).toBeVisible();
  await expect(panel).toContainText("不加百分比余量");
  // Capture the real capability/formula response before substituting sample-task
  // transport, so the deliverable screenshot does not present mock measurements.
  await page.locator(".motion-export").screenshot({
    path: "../workspace/diagnostics/intermediate-output.png",
  });
  const output = {
    master_bit_depth: null,
    video: "prores-4444-acescct",
    render_scale: 2,
  };
  const budget = await call("estimate_motion_render", {
    settings: { width: 64, height: 32, frames: 24, fps: saved.scene.fps },
    output,
    project_id: project.id,
  });
  let starts = 0;
  let complete = true;
  let polls = 0;
  const cancelled: string[] = [];
  // Only task transport is substituted here. Native sampling/encoding/publication
  // has separate Python integration coverage without launching Chrome.
  await page.route(
    "**/api/operations/start_motion_render_estimate",
    async (route) => {
      starts += 1;
      expect(route.request().postDataJSON().spec.code).toBe(saved.scene.code);
      await route.fulfill({
        json: {
          estimate: {
            id: `sample-${starts}`,
            status: "running",
            completed_frames: 0,
            sample_count: 12,
          },
        },
      });
    },
  );
  await page.route(
    "**/api/operations/get_motion_render_estimate",
    async (route) => {
      polls += 1;
      const id = route.request().postDataJSON().estimate_id;
      await route.fulfill({
        json: {
          estimate: {
            id,
            status: cancelled.includes(id)
              ? "cancelled"
              : complete && polls > 1
                ? "completed"
                : "running",
            completed_frames: complete ? 12 : 1,
            sample_count: 12,
            budget: {
              ...budget,
              basis: "sample-mean-v1",
              estimated_output_bytes: 123456,
            },
          },
        },
      });
    },
  );
  await page.route(
    "**/api/operations/cancel_motion_render_estimate",
    async (route) => {
      cancelled.push(route.request().postDataJSON().estimate_id);
      await route.fulfill({
        json: { estimate: { id: cancelled.at(-1), status: "cancelled" } },
      });
    },
  );
  await panel.getByRole("button", { name: "采样估算", exact: true }).click();
  await expect(panel).toContainText("基于 12 帧的采样平均值");
  let submission: Record<string, unknown> | undefined;
  await page.route("**/api/operations/submit_motion_render", async (route) => {
    submission = route.request().postDataJSON();
    await route.fulfill({
      status: 400,
      json: { error: { message: "测试仅检查提交参数，不启动渲染" } },
    });
  });
  await page.getByRole("button", { name: "导出动画", exact: true }).click();
  await expect.poll(() => submission?.storage_estimate_id).toBe("sample-1");
  await expect(
    page.getByRole("alert").filter({ hasText: "测试仅检查提交参数" }),
  ).toBeVisible();
  complete = false;
  await panel.getByRole("button", { name: "采样估算", exact: true }).click();
  await expect(panel.getByRole("button", { name: "取消采样" })).toBeVisible();
  await panel.getByRole("button", { name: "取消采样" }).click();
  await expect(panel).toContainText("采样已取消");
  await panel.getByRole("button", { name: "采样估算", exact: true }).click();
  await expect(panel.getByRole("button", { name: "取消采样" })).toBeVisible();
  // Saving a name refreshes capability discovery, but does not change pixels.
  await page.getByLabel("动画名称", { exact: true }).fill("名称保存保留采样");
  const refreshed = page.waitForResponse("**/api/operations/get_motion_delivery_options");
  await page.getByRole("button", { name: "保存动画", exact: true }).click();
  await refreshed;
  await expect(panel.getByRole("button", { name: "取消采样" })).toBeVisible();
  expect(cancelled).not.toContain("sample-3");
  await page.getByLabel("总帧数", { exact: true }).fill("25");
  await expect.poll(() => cancelled.includes("sample-3")).toBe(true);
  await expect(panel).toContainText("旧采样已失效");
  await expect(page.getByLabel("总帧数", { exact: true })).toHaveValue("25");
  await video.focus();
  await video.press("Space");
  await video.press("Escape");
  await expect(video).toBeFocused();
  await panel.screenshot({
    path: "../workspace/diagnostics/intermediate-desktop.png",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await panel.scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  expect(
    (await new AxeBuilder({ page }).include(".motion-export").analyze())
      .violations,
  ).toEqual([]);
  await panel.screenshot({
    path: "../workspace/diagnostics/intermediate-mobile.png",
  });
  expect((await call("list_jobs", { project_id: project.id })).total).toBe(0);
});
