import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("video-only mode preserves master settings and exports without EXR; accessibility", async ({
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
    name: `仅视频 ${Date.now()}`,
  });
  const blank = await call("create_motion_scene", { project_id: project.id });
  const saved = await call("update_motion_scene", {
    scene_id: blank.record.id,
    expected_revision_id: blank.revision_id,
    spec: { ...blank.scene, width: 32, height: 18, frames: 3 },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await page.getByLabel("动画项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("已保存的动画", { exact: true })
    .selectOption(saved.record.id);
  const mode = page.getByLabel("导出模式", { exact: true });
  const depth = page.getByLabel("EXR 母版精度", { exact: true });
  const video = page.getByLabel("视频输出", { exact: true });
  const budget = page.getByRole("region", { name: "渲染磁盘空间预估" });
  await expect(mode).toHaveValue("master");
  await depth.selectOption("16");
  await video.selectOption("pq");
  await page.getByLabel("正式导出采样", { exact: true }).selectOption("1");
  await mode.focus();
  // Native popup ownership matches the sibling motion controls. macOS headless
  // Chrome does not route arrow keys into its OS menu; assert focus restoration.
  await mode.press("Space");
  await mode.press("Escape");
  await expect(mode).toBeFocused();
  await mode.selectOption("video-only");
  await expect(mode).toHaveValue("video-only");
  await expect(depth).toHaveCount(0);
  await expect(video).toHaveValue("pq");
  await expect(video.locator('option[value="none"]')).toHaveCount(0);
  await expect(
    budget.getByText("不生成（仅视频）", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "交付导出位置" }),
  ).toContainText("仅视频模式不写入 EXR 母版");
  await mode.selectOption("master");
  await expect(depth).toHaveValue("16");
  await expect(video).toHaveValue("pq");
  await video.selectOption("none");
  await mode.selectOption("video-only");
  await expect(video).toHaveValue("srgb");
  const rediscovery = page.waitForResponse((r) =>
    r.url().endsWith("/get_motion_delivery_options"),
  );
  await page
    .getByRole("button", { name: "刷新动画列表与连接", exact: true })
    .click();
  await rediscovery;
  await expect(mode).toHaveValue("video-only");
  await expect(video).toHaveValue("srgb");
  // Failed submission retains the chosen mode and must not create a hidden job.
  await page.route("**/api/operations/submit_motion_render", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "测试导出暂不可用" } },
    }),
  );
  await page.getByRole("button", { name: "导出动画", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "测试导出暂不可用" }),
  ).toBeVisible();
  await expect(mode).toHaveValue("video-only");
  expect((await call("list_jobs", { project_id: project.id })).total).toBe(0);
  await page.unroute("**/api/operations/submit_motion_render");

  await page.setViewportSize({ width: 390, height: 844 });
  await mode.scrollIntoViewIfNeeded();
  await expect(mode).toBeVisible();
  const accessibility = await new AxeBuilder({ page })
    .include(".motion-export")
    .analyze();
  expect(accessibility.violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  await page
    .locator(".motion-export")
    .screenshot({ path: "/private/tmp/chromascript-video-only.png" });
  const request = page.waitForRequest((r) =>
    r.url().endsWith("/submit_motion_render"),
  );
  await page.getByRole("button", { name: "导出动画", exact: true }).click();
  expect((await request).postDataJSON().output).toEqual({
    master_bit_depth: null,
    video: "srgb",
    render_scale: 1,
  });
  let result: { job: { status: string }; artifacts: { kind: string }[] };
  await expect
    .poll(
      async () => {
        const jobs = await call("list_jobs", { project_id: project.id });
        if (!jobs.items?.length) return "waiting";
        result = await call("get_job", { job_id: jobs.items[0].id });
        return result.job.status;
      },
      { timeout: 30000 },
    )
    .toBe("completed");
  expect(result!.artifacts.some((a) => a.kind === "video")).toBe(true);
  expect(result!.artifacts.some((a) => a.kind.startsWith("master"))).toBe(
    false,
  );
  await expect(
    page.getByText("下载 ACEScg 母版 ZIP", { exact: true }),
  ).toHaveCount(0);
});
