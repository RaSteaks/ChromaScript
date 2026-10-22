import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";
import type { SceneReply } from "../src/types";

// Use the real 720p native renderer with a small, time-varying shader. Intercept only
// timing/failures when a test needs deterministic concurrency or recovery evidence.
async function scene(page: Page) {
  const session = await (await page.request.get("/api/session")).json();
  async function call<T>(name: string, args: unknown = {}): Promise<T> {
    const response = await page.request.post("/api/operations/" + name, {
      headers: { Authorization: "Bearer " + session.token },
      data: args,
    });
    expect(response.ok()).toBe(true);
    return response.json();
  }
  const project = await call<{ project: { id: string } }>("create_project", {
    name: "实时预览验证 " + Date.now(),
  });
  const created = await call<SceneReply>("create_scene", {
    project_id: project.project.id,
    spec: {
      name: "实时预览测试",
      code: "fn shade(ctx: SceneContext) -> vec4<f32> { return vec4<f32>(ctx.params.exposure, ctx.time * 0.1 + 0.1, 0.02, 0.5); }",
      parameters: [
        {
          name: "exposure",
          label: "亮度",
          default: 0.18,
          minimum: 0,
          maximum: 4,
        },
      ],
    },
  });
  await page.goto("/");
  await expect(page.locator(".engine-state")).toContainText("Metal");
  await page
    .getByLabel("当前项目", { exact: true })
    .selectOption(project.project.id);
  await page
    .getByRole("button", { name: "继续编辑与预览", exact: true })
    .click();
  const image = page.locator(".live-preview-image");
  await expect(image).toBeVisible();
  await expect(page.getByText("实时预览已更新", { exact: true })).toBeVisible();
  return { call, created, project, image };
}

test("live draft changes update immediately without saving or creating jobs", async ({
  page,
}) => {
  const { call, created, project, image } = await scene(page);
  expect(
    await image.evaluate((img: HTMLImageElement) => img.naturalWidth),
  ).toBe(1280);
  expect(
    await image.evaluate((img: HTMLImageElement) => img.naturalHeight),
  ).toBe(720);
  const original = await image.getAttribute("src");
  const response = page.waitForResponse(
    (r) =>
      r.url().endsWith("/render_live_preview") &&
      r.request().postDataJSON()?.request.spec.values.exposure === 0.8 &&
      r.ok(),
  );
  await page.getByLabel("亮度", { exact: true }).fill("0.8");
  const preview = await (await response).json();
  await expect(image).toHaveAttribute(
    "src",
    "data:image/png;base64," + preview.images[0].data,
  );
  expect(await image.getAttribute("src")).not.toBe(original);
  await expect(page.getByText("未保存", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "渲染当前帧", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("渲染当前帧后可取样", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "在预览中取样，按回车可取中心像素" }),
  ).toHaveCount(0);
  const stored = await call<SceneReply>("get_scene", {
    scene_id: created.record.id,
  });
  expect(stored.revision_id).toBe(created.revision_id);
  expect(stored.scene.values).toEqual({});
  expect(
    (
      await call<{ total: number }>("list_jobs", {
        project_id: project.project.id,
      })
    ).total,
  ).toBe(0);

  await page.getByLabel("当前帧", { exact: true }).fill("60");
  await expect(page.locator(".live-preview-status")).toContainText("帧 60");
  const alpha = await image.evaluate(async (img: HTMLImageElement) => {
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const context = canvas.getContext("2d")!;
    context.drawImage(img, 0, 0);
    return context.getImageData(10, 10, 1, 1).data[3];
  });
  expect(alpha).toBe(128);
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/live-preview-desktop.png",
    fullPage: true,
  });
});

test("fixed ACEScg generation and sRGB preview remain independent of delivery", async ({
  page,
}) => {
  const { image } = await scene(page);
  const original = await image.getAttribute("src");
  const workflow = page.getByRole("region", {
    name: "色彩工作流程",
    exact: true,
  });
  await expect(workflow).toContainText("ACEScg · AP1 · 线性（固定）");
  await expect(workflow).toContainText("sRGB · ACES 2.0 显示变换");
  await expect(workflow.locator("select, input")).toHaveCount(0);
  await page.getByRole("button", { name: "3. 设置交付", exact: true }).click();
  await expect(workflow).toBeVisible();
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  await target
    .getByLabel("视频 / 图像编码", { exact: true })
    .selectOption("hevc");
  await target
    .getByLabel("输出色彩空间", { exact: true })
    .selectOption("Rec.2020");
  await expect(
    target.getByLabel("传递函数 / Gamma", { exact: true }),
  ).toHaveValue("st2084");
  await expect(workflow).toContainText("sRGB · ACES 2.0 显示变换");
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
  // Re-rendering after an HDR delivery edit must preserve the same sRGB bytes.
  await expect(page.getByText("实时预览已更新", { exact: true })).toBeVisible();
  await expect(image).toHaveAttribute("src", original!);
  await page.setViewportSize({ width: 390, height: 844 });
  await workflow.scrollIntoViewIfNeeded();
  await expect(workflow).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../reports/color-workflow-mobile.png",
    fullPage: true,
  });
});

test("canvas specifications are edited in preview and shared with delivery", async ({
  page,
}) => {
  const { call, project, image } = await scene(page);
  const settings = page.getByRole("region", { name: "画面规格", exact: true });
  await expect(settings.getByLabel("宽度", { exact: true })).toBeVisible();
  await settings.getByLabel("画面尺寸", { exact: true }).selectOption("竖屏");
  await expect(settings.getByLabel("宽度", { exact: true })).toHaveValue(
    "1080",
  );
  await expect(settings.getByLabel("高度", { exact: true })).toHaveValue(
    "1920",
  );
  await expect
    .poll(() => image.evaluate((img: HTMLImageElement) => img.naturalHeight))
    .toBe(1280);
  await settings.getByLabel("宽度", { exact: true }).fill("320");
  await settings.getByLabel("高度", { exact: true }).fill("180");
  await settings.getByLabel("帧率", { exact: true }).selectOption("24000/1001");
  await settings.getByLabel("总帧数", { exact: true }).fill("48");
  await settings.getByLabel("空间采样", { exact: true }).selectOption("16");
  await settings.getByLabel("起始时间码", { exact: true }).fill("01:02:03:04");
  const response = page.waitForResponse((reply) => {
    if (!reply.url().endsWith("/render_live_preview")) return false;
    const request = reply.request().postDataJSON().request;
    return (
      request.width === 320 &&
      request.height === 180 &&
      request.frame === 24 &&
      reply.ok()
    );
  });
  await page.getByLabel("当前帧", { exact: true }).fill("24");
  const preview = await (await response).json();
  expect(preview.samples).toBe(1);
  await expect(image).toHaveAttribute(
    "src",
    "data:image/png;base64," + preview.images[0].data,
  );
  await expect(
    page.getByLabel("实时预览播放位置", { exact: true }),
  ).toHaveAttribute("max", "47");
  await expect(settings.getByLabel("画面尺寸", { exact: true })).toHaveValue(
    "custom",
  );

  await page.getByRole("button", { name: "3. 设置交付", exact: true }).click();
  const summary = page.getByRole("region", {
    name: "交付画面规格",
    exact: true,
  });
  await expect(summary).toContainText("320 × 180");
  await expect(summary).toContainText("48 帧");
  await expect(summary).toContainText("16 样本 / 像素");
  await expect(summary).toContainText("01:02:03:04");
  await expect(page.getByLabel("宽度", { exact: true })).toHaveCount(0);
  await page
    .getByRole("button", { name: "在预览中修改规格", exact: true })
    .click();
  await expect(settings.getByLabel("帧率", { exact: true })).toHaveValue(
    "24000/1001",
  );
  await expect(settings.getByLabel("总帧数", { exact: true })).toHaveValue(
    "48",
  );
  await settings.getByLabel("宽度", { exact: true }).fill("0");
  await expect(settings.getByRole("alert")).toContainText(
    "尺寸需为 16–8192 的整数",
  );
  await expect(
    page.getByRole("button", { name: "渲染当前帧", exact: true }),
  ).toBeDisabled();
  await settings.getByLabel("宽度", { exact: true }).fill("320");
  await expect(settings.getByRole("alert")).not.toBeVisible();
  await page.getByRole("button", { name: "3. 设置交付", exact: true }).click();
  // Inspect the exact shared submission without starting an unnecessary 48-frame render.
  await page.route("**/api/operations/submit_configured_delivery", (route) =>
    route.fulfill({
      status: 400,
      json: { error: { message: "已截取规格验证请求" } },
    }),
  );
  const submission = page.waitForRequest(
    "**/api/operations/submit_configured_delivery",
  );
  await page.getByRole("button", { name: /^提交交付/ }).click();
  expect((await submission).postDataJSON().settings).toMatchObject({
    width: 320,
    height: 180,
    frames: 48,
    samples: 16,
    fps: { numerator: 24000, denominator: 1001 },
    timecode: "01:02:03:04",
  });
  expect(
    (
      await call<{ total: number }>("list_jobs", {
        project_id: project.project.id,
      })
    ).total,
  ).toBe(0);
});

test("draft preview coalesces edits and ignores delayed stale replies", async ({
  page,
}) => {
  const { image } = await scene(page);
  let release = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let entered = () => {};
  const delayed = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const values: number[] = [];
  let obsolete = "";
  await page.route("**/api/operations/render_live_preview", async (route) => {
    const value = route.request().postDataJSON().request.spec.values.exposure;
    values.push(value);
    const response = await route.fetch();
    if (value === 0.3) {
      obsolete =
        "data:image/png;base64," + (await response.json()).images[0].data;
      entered();
      await gate;
    }
    await route.fulfill({ response });
  });
  const input = page.getByLabel("亮度", { exact: true });
  await input.fill("0.3");
  await delayed;
  await input.fill("0.5");
  await input.fill("0.9");
  // Wait beyond the edit debounce while the first request is still in flight.
  await page.waitForTimeout(300);
  expect(values).toEqual([0.3]);
  release();
  await expect.poll(() => values).toEqual([0.3, 0.9]);
  await expect(page.getByText("实时预览已更新", { exact: true })).toBeVisible();
  expect(await image.getAttribute("src")).not.toBe(obsolete);
  await expect(input).toHaveValue("0.9");
});

test("compile and network failures retain the last frame and recover without losing the draft", async ({
  page,
}) => {
  const { created, image } = await scene(page);
  const original = await image.getAttribute("src");
  const editor = page.getByLabel("WGSL 场景代码");
  const broken = created.scene.code.replace(
    "ctx.params.exposure",
    "missing_symbol",
  );
  await editor.fill(broken);
  await expect(page.getByRole("alert")).toContainText("missing_symbol");
  await expect(page.locator(".preview-stage")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  await expect(image).toHaveAttribute("src", original!);
  await expect(editor).toHaveText(broken, { useInnerText: true });
  await expect(
    page.getByRole("button", { name: "播放实时预览" }),
  ).toBeDisabled();
  await editor.fill(created.scene.code);
  await expect(page.getByRole("alert")).not.toBeVisible();
  await expect(page.getByText("实时预览已更新", { exact: true })).toBeVisible();
  await page.route("**/api/operations/render_live_preview", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "预览服务暂时不可用，请重试。" } },
    }),
  );
  await page.getByLabel("亮度", { exact: true }).fill("0.6");
  await expect(page.getByRole("alert")).toContainText("预览服务暂时不可用");
  await expect(image).toHaveAttribute("src", original!);
  await page.unroute("**/api/operations/render_live_preview");
  await page.getByRole("button", { name: "重试实时预览", exact: true }).click();
  await expect(page.getByRole("alert")).not.toBeVisible();
  await expect(page.getByText("实时预览已更新", { exact: true })).toBeVisible();
  await expect(page.getByLabel("亮度", { exact: true })).toHaveValue("0.6");
  expect(await image.getAttribute("src")).not.toBe(original);
});

test("slow live playback completes frames with no backlog and stops outside the preview", async ({
  page,
}) => {
  await scene(page);
  const frames: number[] = [];
  let concurrent = 0;
  let maximum = 0;
  await page.route("**/api/operations/render_live_preview", async (route) => {
    concurrent++;
    maximum = Math.max(maximum, concurrent);
    frames.push(route.request().postDataJSON().request.frame);
    const response = await route.fetch();
    await new Promise((resolve) => setTimeout(resolve, 230));
    concurrent--;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "播放实时预览", exact: true }).click();
  await expect.poll(() => new Set(frames).size).toBeGreaterThanOrEqual(3);
  await expect
    .poll(async () =>
      Number(
        (await page.locator(".live-preview-status").innerText()).match(
          /帧 (\d+)/,
        )?.[1],
      ),
    )
    .toBeGreaterThan(0);
  expect(maximum).toBe(1);
  await page.getByRole("button", { name: "暂停实时预览", exact: true }).click();
  await expect(page.getByText("实时预览已更新", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "3. 设置交付", exact: true }).click();
  const count = frames.length;
  await page.waitForTimeout(450);
  expect(frames).toHaveLength(count);
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "播放实时预览", exact: true }),
  ).toBeVisible();
});

test("live preview respects IME, keyboard timeline and narrow accessibility", async ({
  page,
}) => {
  await scene(page);
  const requests: unknown[] = [];
  await page.route("**/api/operations/render_live_preview", (route) => {
    requests.push(route.request().postDataJSON());
    return route.continue();
  });
  const timeline = page.getByLabel("实时预览播放位置", { exact: true });
  await timeline.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByLabel("当前帧", { exact: true })).toHaveValue("1");
  await expect(page.locator(".live-preview-status")).toContainText("帧 1");
  const editor = page.getByLabel("WGSL 场景代码");
  const code = await editor.innerText();
  const count = requests.length;
  await editor.dispatchEvent("compositionstart");
  await editor.fill(code + "\n// 中文输入草稿");
  await page.waitForTimeout(300);
  expect(requests).toHaveLength(count);
  await editor.dispatchEvent("compositionend");
  await expect.poll(() => requests.length).toBe(count + 1);
  await expect(page.getByText("实时预览已更新", { exact: true })).toBeVisible();
  // The shared editor stays visible; Tab must still leave its content surface.
  await editor.press("Tab");
  await expect(editor).not.toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const accessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(accessibility.violations).toEqual([]);
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/live-preview-mobile.png",
    fullPage: true,
  });
});

test("hidden documents stop live work and return with playback paused", async ({
  page,
}) => {
  await scene(page);
  let count = 0;
  await page.route("**/api/operations/render_live_preview", (route) => {
    count++;
    return route.continue();
  });
  await page.getByRole("button", { name: "播放实时预览", exact: true }).click();
  await expect.poll(() => count).toBeGreaterThanOrEqual(2);
  // Reproduce the browser visibility lifecycle deterministically in headless
  // Chrome, where opening another page does not consistently background a tab.
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(
    page.getByRole("button", { name: "播放实时预览", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");
  const stopped = count;
  await page.waitForTimeout(400);
  expect(count).toBe(stopped);
  await page.evaluate(() => {
    Reflect.deleteProperty(document, "hidden");
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.getByText("实时预览已更新", { exact: true })).toBeVisible();
  await expect.poll(() => count).toBe(stopped + 1);
  await page.waitForTimeout(300);
  expect(count).toBe(stopped + 1);
});
