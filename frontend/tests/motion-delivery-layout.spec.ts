import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import type { Capability, MotionReply, MotionSpec } from "../src/types";
import { openDelivery, openPreview } from "./workspace-navigation";

async function setup(page: Page, engine: MotionSpec["engine"]) {
  const session = await (await page.request.get("/api/session")).json();
  async function call<T>(operation: string, data: unknown): Promise<T> {
    const reply = await page.request.post(`/api/operations/${operation}`, {
      headers: { Authorization: `Bearer ${session.token}` },
      data,
    });
    expect(reply.ok(), await reply.text()).toBe(true);
    return reply.json();
  }
  const { project } = await call<{ project: { id: string } }>(
    "create_project",
    { name: `交付排版验证 ${engine} ${Date.now()}` },
  );
  const created = await call<MotionReply>("create_motion_scene", {
    project_id: project.id,
    engine,
  });
  const saved = await call<MotionReply>("update_motion_scene", {
    scene_id: created.record.id,
    expected_revision_id: created.revision_id,
    spec: { ...created.scene, width: 320, height: 180, frames: 12 },
  });
  const options = await call<NonNullable<Capability["delivery_export"]>>(
    "get_delivery_export_options",
    {},
  );
  await page.goto("/");
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("当前作品", { exact: true })
    .selectOption(`motion:${saved.record.id}`);
  await openDelivery(page);
  await expect(page.getByLabel("视频输出", { exact: true })).toBeEnabled();
  return { saved, options };
}

for (const engine of ["three-float-v1", "remotion-sdr-v1"] as const) {
  test(`${engine} delivery uses desktop columns and preserves settings when stacked`, async ({
    page,
  }, info) => {
    const { options } = await setup(page, engine);
    const longDirectory = join(
      options.default_directory,
      "动画交付与画面验证-".repeat(6),
    );
    mkdirSync(longDirectory, { recursive: true });
    const directory = page.getByLabel("导出位置", { exact: true });
    await directory.fill(longDirectory);
    if (engine === "three-float-v1") {
      await page.getByLabel("EXR 母版精度", { exact: true }).selectOption("16");
      await page
        .getByLabel("导出模式", { exact: true })
        .selectOption("video-only");
    }
    await expect(page.getByLabel("视频输出", { exact: true })).toHaveValue(
      "srgb",
    );
    const settings = page.getByRole("region", {
      name: "导出设置",
      exact: true,
    });
    const destination = page.getByRole("region", {
      name: "交付导出位置",
      exact: true,
    });
    const budget = page.getByRole("region", {
      name: "渲染磁盘空间预估",
      exact: true,
    });
    await expect(
      budget.getByText("预计磁盘峰值占用", { exact: true }),
    ).toBeVisible();
    // Assert the real column relationship, then the narrow source order: no duplicated controls.
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      const left = (await settings.boundingBox())!;
      const right = (await destination.boundingBox())!;
      if (width > 900) {
        expect(right.x).toBeGreaterThanOrEqual(left.x + left.width);
        expect(Math.abs(right.y - left.y)).toBeLessThan(2);
      } else {
        expect(right.y).toBeGreaterThanOrEqual(left.y + left.height);
        expect(Math.abs(right.x - left.x)).toBeLessThan(2);
      }
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await expect(directory).toHaveCount(1);
      await expect(directory).toHaveValue(longDirectory);
      await expect(page.getByLabel("视频输出", { exact: true })).toHaveValue(
        "srgb",
      );
      expect(
        (await new AxeBuilder({ page }).include(".motion-export").analyze())
          .violations,
      ).toEqual([]);
      await page
        .locator(".motion-export")
        .screenshot({ path: info.outputPath(`delivery-${width}.png`) });
    }
    if (engine === "three-float-v1") {
      await page.getByLabel("导出模式", { exact: true }).selectOption("master");
      await expect(
        page.getByLabel("EXR 母版精度", { exact: true }),
      ).toHaveValue("16");
    }
    await openPreview(page);
    await openDelivery(page);
    await expect(directory).toHaveValue(longDirectory);
    await expect(page.getByLabel("视频输出", { exact: true })).toHaveValue(
      "srgb",
    );
  });
}

test("folder selection, cancellation and failures retain the right-column destination", async ({
  page,
}, info) => {
  const { saved, options } = await setup(page, "three-float-v1");
  const directory = page.getByLabel("导出位置", { exact: true });
  const picker = page.getByRole("button", { name: "选择文件夹", exact: true });
  const submit = page.getByRole("button", { name: "导出动画", exact: true });
  const selected = join(options.default_directory, "测试交付目录");
  mkdirSync(selected, { recursive: true });
  let mode: "selected" | "cancelled" | "failed" = "selected";
  let release: () => void = () => {};
  let gate: Promise<void> = Promise.resolve();
  // Substitute only the OS dialog response; all draft/validation state remains real.
  await page.route(
    "**/api/operations/choose_export_directory",
    async (route) => {
      expect(route.request().postDataJSON().initial_directory).toBe(
        await directory.inputValue(),
      );
      await gate;
      if (mode === "failed")
        return route.fulfill({
          status: 503,
          json: { error: { message: "文件夹暂不可用" } },
        });
      await route.fulfill({
        json:
          mode === "cancelled"
            ? { cancelled: true, directory: null }
            : { cancelled: false, directory: selected },
      });
    },
  );
  gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await picker.click();
  try {
    await expect(picker).toHaveAttribute("aria-busy", "true");
    await expect(submit).toBeDisabled();
  } finally {
    release();
  }
  await expect(directory).toHaveValue(selected);
  mode = "cancelled";
  await picker.focus();
  await picker.press("Enter");
  await expect(picker).toBeEnabled();
  await expect(directory).toHaveValue(selected);
  mode = "failed";
  await picker.click();
  await expect(
    page.getByRole("alert").filter({ hasText: "文件夹暂不可用" }),
  ).toBeVisible();
  await expect(directory).toHaveValue(selected);
  await page.getByRole("button", { name: "使用默认位置", exact: true }).click();
  await expect(directory).toHaveValue(options.default_directory);
  let submitted: Record<string, unknown> | undefined;
  // Verify submission after the layout change without starting a Chrome export worker.
  await page.route("**/api/operations/submit_motion_render", async (route) => {
    submitted = route.request().postDataJSON();
    await route.fulfill({
      status: 503,
      json: { error: { message: "布局测试仅验证提交参数" } },
    });
  });
  await submit.click();
  await expect
    .poll(() => submitted?.export_directory)
    .toBe(options.default_directory);
  expect(submitted?.scene_id).toBe(saved.record.id);
  expect(submitted?.expected_revision_id).toBe(saved.revision_id);
  await expect(directory).toHaveValue(options.default_directory);
  await page
    .locator(".motion-export")
    .screenshot({ path: info.outputPath("delivery-errors.png") });
});
