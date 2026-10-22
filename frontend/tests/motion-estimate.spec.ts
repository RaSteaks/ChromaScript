import { openDelivery, openPreview } from "./workspace-navigation";
import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const format = (value: number) =>
  new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(
    value / 1024 ** 2,
  ) + " MiB";

test("master storage follows drafts, rejects stale replies, recovers and stays accessible", async ({
  page,
}) => {
  const session = await (await page.request.get("/api/session")).json();
  async function call(name: string, data: unknown) {
    const result = await page.request.post(`/api/operations/${name}`, {
      headers: { Authorization: `Bearer ${session.token}` },
      data,
    });
    expect(result.ok(), await result.text()).toBe(true);
    return result.json();
  }
  const { project } = await call("create_project", {
    name: `磁盘预估 ${Date.now()}`,
  });
  const blank = await call("create_motion_scene", { project_id: project.id });
  const saved = await call("update_motion_scene", {
    scene_id: blank.record.id,
    expected_revision_id: blank.revision_id,
    spec: { ...blank.scene, width: 320, height: 180, frames: 30 },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("当前作品", { exact: true })
    .selectOption(`motion:${saved.record.id}`);
  await openDelivery(page);
  const panel = page.getByRole("region", { name: "渲染磁盘空间预估" });
  const master = panel
    .locator("dl > div")
    .filter({ has: page.getByText("EXR 母版（未压缩）", { exact: true }) })
    .locator("dd");
  await expect(master).toHaveText(format(320 * 180 * 16 * 30));
  await page.getByLabel("EXR 母版精度", { exact: true }).selectOption("16");
  await expect(master).toHaveText(format(320 * 180 * 8 * 30));
  await page.getByLabel("正式导出采样", { exact: true }).selectOption("4");
  await expect(
    panel.getByText("正在预估磁盘空间…", { exact: true }),
  ).toBeVisible();
  await expect(master).toHaveText(format(320 * 180 * 8 * 30));
  await panel.getByText("查看空间预算明细", { exact: true }).click();
  await expect(
    panel
      .locator("dl > div")
      .filter({ has: page.getByText("中间数据预算", { exact: true }) })
      .locator("dd"),
  ).toHaveText(format(320 * 180 * 16 * 16));

  // A late 60-frame response must not replace the newest 90-frame draft estimate.
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let delayed = false;
  await page.route(
    "**/api/operations/estimate_motion_render",
    async (route) => {
      if (route.request().postDataJSON().settings.frames === 60) {
        delayed = true;
        const response = await route.fetch();
        await gate;
        await route.fulfill({ response });
      } else await route.continue();
    },
  );
  await openPreview(page);
  await page.getByLabel("总帧数", { exact: true }).fill("60");
  await openDelivery(page);
  await expect.poll(() => delayed).toBe(true);
  await openPreview(page);
  await page.getByLabel("总帧数", { exact: true }).fill("90");
  await openDelivery(page);
  await expect(master).toHaveText(format(320 * 180 * 8 * 90));
  release();
  await expect(master).toHaveText(format(320 * 180 * 8 * 90));
  await page.unroute("**/api/operations/estimate_motion_render");
  await openPreview(page);
  await page.getByLabel("总帧数", { exact: true }).fill("");
  await openDelivery(page);
  await expect(
    panel.getByText("填写有效的画面规格与导出位置后显示预估。"),
  ).toBeVisible();
  await expect(master).toHaveCount(0);
  await openPreview(page);
  await page.getByLabel("总帧数", { exact: true }).fill("90");
  await openDelivery(page);
  await expect(master).toHaveText(format(320 * 180 * 8 * 90));

  await page.route("**/api/operations/estimate_motion_render", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "磁盘暂时不可用" } },
    }),
  );
  await panel.getByRole("button", { name: "刷新预估" }).click();
  await expect(panel.getByRole("alert")).toContainText("磁盘暂时不可用");
  await expect(page.getByLabel("总帧数", { exact: true })).toHaveValue("90");
  await page.unroute("**/api/operations/estimate_motion_render");
  await page.route(
    "**/api/operations/estimate_motion_render",
    async (route) => {
      const response = await route.fetch();
      await route.fulfill({
        json: {
          ...(await response.json()),
          destination_free_bytes: 0,
          destination_sufficient: false,
          sufficient: false,
        },
      });
    },
  );
  await panel.getByRole("button", { name: "刷新预估" }).click();
  await expect(panel.getByRole("alert")).toContainText("导出盘空间不足");
  await page.unroute("**/api/operations/estimate_motion_render");
  await panel.getByRole("button", { name: "刷新预估" }).click();
  await expect(panel.getByRole("alert")).toHaveCount(0);
  await expect(master).toHaveText(format(320 * 180 * 8 * 90));
  await page.setViewportSize({ width: 390, height: 844 });
  await panel.scrollIntoViewIfNeeded();
  const accessibility = await new AxeBuilder({ page })
    .include(".motion-storage-estimate")
    .analyze();
  expect(accessibility.violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  await panel.screenshot({
    path: "/private/tmp/chromascript-master-estimate.png",
  });
  expect((await call("list_jobs", { project_id: project.id })).total).toBe(0);
  // Estimation never saves the numeric draft or creates a render task.
  expect(
    (await call("get_motion_scene", { scene_id: saved.record.id })).scene
      .frames,
  ).toBe(30);
});
