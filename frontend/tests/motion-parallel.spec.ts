import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { fileURLToPath } from "node:url";
import { openDelivery } from "./workspace-navigation";

test("parallel delivery settings preserve drafts, budgets and keyboard; accessibility", async ({
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
    name: `并行设置 ${Date.now()}`,
  });
  const created = await call("create_motion_scene", { project_id: project.id });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("当前作品", { exact: true })
    .selectOption(`motion:${created.record.id}`);
  await openDelivery(page);
  const workers = page.getByLabel("渲染并发", { exact: true });
  await expect(workers).toHaveValue("1");
  await expect(workers.locator("option")).toHaveCount(3);
  await workers.focus();
  await workers.press("Space");
  await workers.press("Escape");
  await expect(workers).toBeFocused();
  const estimate = page.waitForRequest(
    (request) =>
      request.url().endsWith("/estimate_motion_render") &&
      request.postDataJSON().output.render_workers === 4,
  );
  await workers.selectOption("4");
  await estimate;
  await page.getByLabel("导出模式", { exact: true }).selectOption("video-only");
  await expect(workers).toHaveValue("4");
  await expect(
    page.getByRole("region", { name: "渲染磁盘空间预估" }),
  ).toContainText("每路最多保留一帧");
  await page.getByRole("button", { name: "刷新作品列表", exact: true }).click();
  await expect(workers).toHaveValue("4");
  await page.locator(".motion-export").screenshot({
    path: fileURLToPath(
      new URL("../../reports/parallel-export-desktop.png", import.meta.url),
    ),
  });
  // Intercept all submissions: UI tests must not launch the production Chrome driver.
  await page.route("**/api/operations/submit_motion_render", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "测试并行导出暂不可用" } },
    }),
  );
  await page.getByRole("button", { name: "导出动画", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "测试并行导出暂不可用" }),
  ).toBeVisible();
  await expect(workers).toHaveValue("4");
  expect((await call("list_jobs", { project_id: project.id })).total).toBe(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await workers.scrollIntoViewIfNeeded();
  await expect(workers).toBeVisible();
  await workers.selectOption("2");
  const accessibility = await new AxeBuilder({ page })
    .include(".motion-export")
    .analyze();
  expect(accessibility.violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  await page.locator(".motion-export").screenshot({
    path: fileURLToPath(
      new URL("../../reports/parallel-export-mobile.png", import.meta.url),
    ),
  });
  await page.unroute("**/api/operations/submit_motion_render");
  const fakeJob = {
    id: "parallel-ui-accepted",
    project_id: project.id,
    scene_id: created.record.id,
    kind: "motion",
    status: "queued",
    stage: "queued",
    progress: 0,
    completed_frames: 0,
    artifacts: [],
    settings: { width: 1920, height: 1080 },
  };
  await page.route("**/api/operations/submit_motion_render", (route) =>
    route.fulfill({ json: { job: fakeJob } }),
  );
  await page.route("**/api/operations/get_job", (route) =>
    route.fulfill({ json: { job: fakeJob, artifacts: [] } }),
  );
  const submitted = page.waitForRequest((request) =>
    request.url().endsWith("/submit_motion_render"),
  );
  await page.getByRole("button", { name: "导出动画", exact: true }).click();
  expect((await submitted).postDataJSON().output.render_workers).toBe(2);
  expect((await call("list_jobs", { project_id: project.id })).total).toBe(0);
});
