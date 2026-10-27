import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { fileURLToPath } from "node:url";

for (const engine of ["motion", "wgsl"] as const) {
  test(`${engine} task elapsed clock advances, freezes, resets and remains visible on narrow screens; accessibility`, async ({
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
      name: `任务用时 ${engine} ${Date.now()}`,
    });
    const created = await call(
      engine === "motion" ? "create_motion_scene" : "create_scene",
      {
        project_id: project.id,
        ...(engine === "wgsl" ? { template_id: "gradient" } : {}),
      },
    );
    const sceneId = created.record.id;
    let status = "running";
    let stage = "encoding";
    let elapsed = 62;
    let updated = new Date().toISOString();
    const job = () => ({
      id: "elapsed-active",
      project_id: project.id,
      scene_id: sceneId,
      revision_id: created.revision_id,
      kind: engine === "motion" ? "motion" : "delivery",
      status,
      stage,
      elapsed_seconds: elapsed,
      updated_at: updated,
      progress: 0.5,
      completed_frames: 300,
      frame_indices: null,
      artifacts: [],
      error: null,
      settings: {
        width: 64,
        height: 36,
        frames: 600,
        fps: { numerator: 60, denominator: 1 },
        samples: 1,
        start_frame: 0,
      },
    });
    const queued = () => ({
      ...job(),
      id: "elapsed-queued",
      status: "queued",
      stage: "queued",
      elapsed_seconds: 62,
    });
    // Read-only fixtures never enqueue a renderer or touch the user's projects.
    await page.route("**/api/operations/list_jobs", (route) =>
      route.fulfill({ json: { items: [job(), queued()], total: 2 } }),
    );
    await page.route("**/api/operations/get_job", (route) =>
      route.fulfill({
        json: {
          job:
            route.request().postDataJSON().job_id === "elapsed-queued"
              ? queued()
              : job(),
          artifacts: [],
        },
      }),
    );
    await page.goto("/");
    await page.getByRole("button", { name: "工作区", exact: true }).click();
    await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
    await page
      .getByLabel("当前作品", { exact: true })
      .selectOption(`${engine === "motion" ? "motion" : "wgsl"}:${sceneId}`);
    const openJobs = () =>
      page.getByRole("button", { name: "5. 任务与成果", exact: true }).click();
    await openJobs();
    if (engine === "motion")
      await page
        .getByLabel("动画渲染任务", { exact: true })
        .selectOption("elapsed-active");
    const region =
      engine === "motion"
        ? page.locator(".motion-results")
        : page.locator(".job-row").first();
    const timer = region.getByRole("timer", { name: "任务渲染用时" });
    await expect(timer).toBeVisible();
    await expect(timer).toContainText("用时 00:01:");
    const before = await timer.textContent();
    await expect(timer).not.toHaveText(before!);
    expect(await timer.getAttribute("aria-live")).toBe("off");

    async function refresh() {
      // Scene switches reconcile job reads through the real UI subscriptions.
      await page.getByLabel("当前项目", { exact: true }).selectOption("");
      await page
        .getByLabel("当前项目", { exact: true })
        .selectOption(project.id);
      await page
        .getByLabel("当前作品", { exact: true })
        .selectOption(`${engine === "motion" ? "motion" : "wgsl"}:${sceneId}`);
      await openJobs();
      if (engine === "motion")
        await page
          .getByLabel("动画渲染任务", { exact: true })
          .selectOption("elapsed-active");
    }
    for (const terminal of [
      "completed",
      "failed",
      "cancelled",
      "interrupted",
    ]) {
      status = terminal;
      stage = terminal;
      elapsed = 3665.9;
      await refresh();
      await expect(timer).toHaveText("用时 01:01:05");
    }
    await page.waitForTimeout(1200);
    await expect(timer).toHaveText("用时 01:01:05");
    status = "running";
    stage = "starting";
    updated = new Date().toISOString();
    await refresh();
    await expect(timer).toContainText("用时 00:00:");
    if (engine === "motion") {
      await page
        .getByLabel("动画渲染任务", { exact: true })
        .selectOption("elapsed-queued");
      await expect(timer).toHaveText("用时 —");
      await page
        .getByLabel("动画渲染任务", { exact: true })
        .selectOption("elapsed-active");
    } else
      await expect(
        page.getByRole("timer").filter({ hasText: "用时 —" }),
      ).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    await timer.scrollIntoViewIfNeeded();
    await expect(timer).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
    const scope = engine === "motion" ? ".motion-results" : ".job-panel";
    expect(
      (await new AxeBuilder({ page }).include(scope).analyze()).violations,
    ).toEqual([]);
    await page
      .locator(scope)
      .screenshot({
        path: fileURLToPath(
          new URL(`../../reports/job-elapsed-${engine}.png`, import.meta.url),
        ),
      });
  });
}
