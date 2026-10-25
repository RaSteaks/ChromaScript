// Five-stage navigation includes Sound before delivery.
import { openExamples } from "./workspace-navigation";
import { test, expect, type Page, type Locator } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";
import type { ColorAppearance, SceneReply } from "../src/types";

// Seed a constant-color shader so a real GPU preview can be compared with the
// chosen display swatch, independently of a template's lighting/exposure effects.
async function scene(page: Page, space = "ACEScg", rgb = [0.8, 0.25, 0.035]) {
  const session = await (await page.request.get("/api/session")).json();
  async function call<T>(operation: string, args: unknown = {}): Promise<T> {
    const response = await page.request.post("/api/operations/" + operation, {
      headers: { Authorization: "Bearer " + session.token },
      data: args,
    });
    expect(response.ok()).toBe(true);
    return response.json();
  }
  const project = await call<{ project: { id: string } }>("create_project", {
    name: "选色验证 " + Date.now(),
  });
  const created = await call<SceneReply>("create_scene", {
    project_id: project.project.id,
    spec: {
      name: "感知选色验证",
      code: "fn shade(ctx: SceneContext) -> vec4<f32> { return vec4<f32>(ctx.params.color, 1.0); }",
      parameters: [
        {
          name: "color",
          label: "颜色",
          kind: "color",
          color_space: space,
          default: rgb,
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
  await expect(
    page.getByRole("button", { name: "颜色 选取颜色" }),
  ).toBeVisible();
  return { call, created };
}

async function open(page: Page) {
  await page
    .getByRole("button", { name: "颜色 选取颜色", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "颜色 · 感知选色" });
  await expect(dialog.getByLabel("色相 H", { exact: true })).toBeVisible();
  return dialog;
}

async function choose(page: Page, dialog: Locator) {
  const response = page.waitForResponse(
    (r) =>
      r.url().endsWith("/resolve_color") &&
      !!r.request().postDataJSON()?.request?.oklch &&
      r.ok(),
  );
  const plane = dialog.getByRole("button", { name: "明度与彩度选色板" });
  const box = (await plane.boundingBox())!;
  await plane.click({ position: { x: box.width * 0.3, y: box.height * 0.4 } });
  const result: ColorAppearance = await (await response).json();
  await expect(
    dialog.getByRole("button", { name: "应用颜色", exact: true }),
  ).toBeEnabled();
  return result;
}

test("visual color selection saves scene RGB and matches the real ACES GPU preview", async ({
  page,
}) => {
  const { call, created } = await scene(page);
  const dialog = await open(page);
  await expect(dialog.getByRole("button", { name: "应用颜色" })).toBeDisabled();
  const picked = await choose(page, dialog);
  // Canvas palette math must match the service too, including the visibly
  // hatched gamut boundary. Allow integer canvas quantization plus OCIO error.
  const samples = await dialog
    .locator("canvas")
    .evaluate((node: HTMLCanvasElement) => {
      const ctx = node.getContext("2d")!;
      return [
        [0, 0],
        [10, 80],
        [80, 80],
        [200, 80],
        [0, 159],
      ].map(([x, y]) => ({
        x,
        y,
        pixel: [...ctx.getImageData(x, y, 1, 1).data],
        width: node.width,
        height: node.height,
      }));
    });
  for (const sample of samples) {
    const expected = await call<ColorAppearance>("resolve_color", {
      request: {
        oklch: {
          lightness: 1 - sample.y / (sample.height - 1),
          chroma: (sample.x / (sample.width - 1)) * 0.4,
          hue: picked.oklch.hue,
        },
      },
    });
    const hatch = expected.gamut_mapped && (sample.x + sample.y) % 10 < 2;
    for (let c = 0; c < 3; c++)
      expect(
        Math.abs(
          sample.pixel[c] / 255 - expected.display_rgb[c] * (hatch ? 0.72 : 1),
        ),
      ).toBeLessThan(2 / 255);
  }
  await dialog.getByRole("button", { name: "应用颜色", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByText("未保存", { exact: true })).toBeVisible();
  // Applying stays a local draft until the existing revision-checked save.
  expect(
    (await call<SceneReply>("get_scene", { scene_id: created.record.id })).scene
      .values,
  ).toEqual({});
  await page.getByRole("button", { name: "保存作品", exact: true }).click();
  await expect(page.getByText("保存场景已完成", { exact: true })).toBeVisible();
  const saved = await call<SceneReply>("get_scene", {
    scene_id: created.record.id,
  });
  expect(saved.scene.values.color).toEqual(picked.rgb);

  // Logical canvas dimensions are edited in the preview inspector itself.
  await page.getByLabel("宽度", { exact: true }).fill("32");
  await page.getByLabel("高度", { exact: true }).fill("32");
  await page.getByRole("button", { name: "渲染所选帧", exact: true }).click();
  const preview = page.locator(".preview-stage img");
  await expect(preview).toBeVisible({ timeout: 20000 });
  const actual = await preview.evaluate(async (node: HTMLImageElement) => {
    await node.decode();
    const canvas = document.createElement("canvas");
    canvas.width = node.naturalWidth;
    canvas.height = node.naturalHeight;
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(node, 0, 0);
    return [...ctx.getImageData(16, 16, 1, 1).data];
  });
  for (let c = 0; c < 3; c++)
    expect(Math.abs(actual[c] / 255 - picked.display_rgb[c])).toBeLessThan(
      1 / 255,
    );
  expect(actual[3]).toBe(255);
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/color-picker-scene.png",
    fullPage: true,
  });
});

test("cancel, Escape and navigation preserve original negative and HDR floats", async ({
  page,
}) => {
  const original = [-0.123456789123, 16.987654321, 0.0000000123456789];
  const { call, created } = await scene(page, "ACEScg", original);
  const trigger = page.getByRole("button", {
    name: "颜色 选取颜色",
    exact: true,
  });
  const dialog = await open(page);
  await expect(dialog.getByText(/原始值含负值或大于 1/)).toBeVisible();
  await choose(page, dialog);
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();
  await open(page);
  await choose(page, dialog);
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByText("未保存", { exact: true })).not.toBeVisible();
  expect(
    (await call<SceneReply>("get_scene", { scene_id: created.record.id })).scene
      .parameters[0].default,
  ).toEqual(original);
  if ((await page.locator(".color-advanced").getAttribute("open")) === null)
    await page.locator(".color-advanced summary").click();
  for (const [i, channel] of ["R", "G", "B"].entries())
    await expect(
      page.getByLabel("颜色 " + channel, { exact: true }),
    ).toHaveValue(String(original[i]));
  // Incomplete advanced input must not be silently converted to 0.
  await page.getByLabel("颜色 R", { exact: true }).fill("");
  await expect(
    page.getByText("请输入三个有限的浮点数；未完成的输入尚未应用。"),
  ).toBeVisible();
  await open(page);
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("颜色 R", { exact: true })).toHaveValue("");
  await page.getByLabel("颜色 R", { exact: true }).fill("-0.25");
  await page.getByRole("button", { name: "4. 设置交付", exact: true }).click();
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
  if ((await page.locator(".color-advanced").getAttribute("open")) === null)
    await page.locator(".color-advanced summary").click();
  await expect(page.getByLabel("颜色 R", { exact: true })).toHaveValue("-0.25");
});

test("incomplete RGB drafts survive stage and MCP navigation and guard replacement", async ({
  page,
}) => {
  const original = [-0.123456789123, 16.987654321, 0.0000000123456789];
  const { call, created } = await scene(page, "ACEScg", original);
  if ((await page.locator(".color-advanced").getAttribute("open")) === null)
    await page.locator(".color-advanced summary").click();
  await page.getByLabel("颜色 R", { exact: true }).fill("");
  // Each view unmounts ColorField. Preserve the raw input and its error rather
  // than rebuilding it from the last valid numeric RGB stored in the scene.
  for (const name of ["4. 设置交付", "5. 任务与成果", "1. 选择作品"]) {
    await page.getByRole("button", { name, exact: true }).click();
    await page
      .getByRole("button", { name: "2. 编辑与预览", exact: true })
      .click();
    if ((await page.locator(".color-advanced").getAttribute("open")) === null)
      await page.locator(".color-advanced summary").click();
    await expect(page.getByLabel("颜色 R", { exact: true })).toHaveValue("");
    await expect(page.getByLabel("颜色 R", { exact: true })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  }
  await page.getByRole("button", { name: "MCP 接口", exact: true }).click();
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  if ((await page.locator(".color-advanced").getAttribute("open")) === null)
    await page.locator(".color-advanced summary").click();
  await expect(page.getByLabel("颜色 R", { exact: true })).toHaveValue("");
  for (const [i, channel] of ["G", "B"].entries())
    await expect(
      page.getByLabel("颜色 " + channel, { exact: true }),
    ).toHaveValue(String(original[i + 1]));
  await expect(page.getByText("未保存", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "保存作品", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText([
    "请先完成高级 RGB 输入，再保存场景。",
    "请输入三个有限的浮点数；未完成的输入尚未应用。",
  ]);
  expect(
    (await call<SceneReply>("get_scene", { scene_id: created.record.id }))
      .revision_id,
  ).toBe(created.revision_id);
  await page.getByRole("button", { name: "1. 选择作品", exact: true }).click();
  await openExamples(page);
  await page.getByRole("button", { name: /广色域渐变/ }).click();
  const discard = page.getByRole("dialog", { name: "保留未保存修改" });
  await expect(discard).toBeVisible();
  await discard.getByRole("button", { name: "继续编辑", exact: true }).click();
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
  if ((await page.locator(".color-advanced").getAttribute("open")) === null)
    await page.locator(".color-advanced summary").click();
  await expect(page.getByLabel("颜色 R", { exact: true })).toHaveValue("");
  await page.getByLabel("颜色 R", { exact: true }).fill("-0.25");
  await page.getByRole("button", { name: "保存作品", exact: true }).click();
  await expect(page.getByText("保存场景已完成", { exact: true })).toBeVisible();
  const saved = await call<SceneReply>("get_scene", {
    scene_id: created.record.id,
  });
  expect(saved.scene.values.color).toEqual([-0.25, ...original.slice(1)]);
  await page.getByLabel("颜色 R", { exact: true }).fill("");
  await page.getByRole("button", { name: "重新载入", exact: true }).click();
  await discard
    .getByRole("button", { name: "切换并舍弃", exact: true })
    .click();
  await expect(page.getByLabel("颜色 R", { exact: true })).toHaveValue("-0.25");
  await expect(page.getByText("未保存", { exact: true })).not.toBeVisible();
});

test("failed and delayed color conversions preserve candidate and block stale Apply", async ({
  page,
}) => {
  const { call, created } = await scene(page);
  const dialog = await open(page);
  await page.route("**/api/operations/resolve_color", async (route) => {
    if (!route.request().postDataJSON().request.oklch) return route.continue();
    return route.fulfill({
      status: 503,
      json: { error: { message: "选色服务暂时不可用" } },
    });
  });
  const hue = dialog.getByLabel("色相 H", { exact: true });
  await hue.focus();
  await hue.press("End");
  await expect(dialog.getByRole("alert")).toContainText("选色服务暂时不可用");
  await expect(dialog.getByRole("button", { name: "应用颜色" })).toBeDisabled();
  await expect(hue).toHaveValue("360");
  await page.unroute("**/api/operations/resolve_color");
  await dialog.getByRole("button", { name: "重试选色换算" }).click();
  await expect(hue).toHaveValue("360");
  await expect(dialog.getByRole("button", { name: "应用颜色" })).toBeEnabled();

  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let entered: () => void = () => {};
  const delayed = new Promise<void>((resolve) => {
    entered = resolve;
  });
  await page.route("**/api/operations/resolve_color", async (route) => {
    if (route.request().postDataJSON().request.oklch?.hue !== 0)
      return route.continue();
    const response = await route.fetch();
    entered();
    await gate;
    // The browser is allowed to abort the superseded conversion request.
    await route.fulfill({ response }).catch(() => {});
  });
  await hue.press("Home");
  await delayed;
  await expect(dialog.getByRole("button", { name: "应用颜色" })).toBeDisabled();
  const latestResponse = page.waitForResponse(
    (r) =>
      r.url().endsWith("/resolve_color") &&
      r.request().postDataJSON()?.request?.oklch?.hue === 1 &&
      r.ok(),
  );
  await hue.press("ArrowRight");
  const latest: ColorAppearance = await (await latestResponse).json();
  await expect(dialog.getByRole("button", { name: "应用颜色" })).toBeEnabled();
  release();
  await expect(hue).toHaveValue("1");
  await dialog.getByRole("button", { name: "应用颜色" }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: "保存作品", exact: true }).click();
  await expect(page.getByText("保存场景已完成", { exact: true })).toBeVisible();
  expect(
    (await call<SceneReply>("get_scene", { scene_id: created.record.id })).scene
      .values.color,
  ).toEqual(latest.rgb);
});

test("unavailable initial appearance keeps RGB accessible and supports retry", async ({
  page,
}) => {
  const original = [-0.25, 4, 0.0123456789];
  await scene(page, "ACEScg", original);
  await page.route("**/api/operations/resolve_color", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "颜色服务暂时不可用" } },
    }),
  );
  await page
    .getByRole("button", { name: "颜色 选取颜色", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "颜色 · 感知选色" });
  await expect(dialog.getByRole("alert")).toContainText("颜色服务暂时不可用");
  await expect(dialog.locator(".color-picker")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  await expect(dialog.getByRole("button", { name: "应用颜色" })).toBeDisabled();
  await page.unroute("**/api/operations/resolve_color");
  await dialog.getByRole("button", { name: "重试选色换算" }).click();
  await expect(dialog.getByLabel("明度 L", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  if ((await page.locator(".color-advanced").getAttribute("open")) === null)
    await page.locator(".color-advanced summary").click();
  await expect(page.getByLabel("颜色 R", { exact: true })).toHaveValue("-0.25");
  await expect(page.getByLabel("颜色 G", { exact: true })).toHaveValue("4");
  await expect(page.getByText("未保存", { exact: true })).not.toBeVisible();
});

test("picker accessibility, gamut feedback, keyboard and narrow viewport", async ({
  page,
}) => {
  await scene(page, "sRGB");
  const dialog = await open(page);
  const hue = dialog.getByLabel("色相 H", { exact: true });
  await hue.focus();
  await hue.press("ArrowRight");
  const chroma = dialog.getByLabel("彩度 C", { exact: true });
  await chroma.focus();
  await chroma.press("End");
  await expect(dialog.getByText(/超出 sRGB 色域/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "应用颜色" })).toBeEnabled();
  const before = await dialog
    .getByLabel("明度 L", { exact: true })
    .inputValue();
  await dialog.getByRole("button", { name: "明度与彩度选色板" }).focus();
  await page.keyboard.press("ArrowDown");
  await expect(dialog.getByLabel("明度 L", { exact: true })).not.toHaveValue(
    before,
  );
  await expect(dialog.getByRole("button", { name: "应用颜色" })).toBeEnabled();
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/color-picker-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(
    await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth),
  ).toBe(true);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await dialog
    .getByRole("button", { name: "应用颜色" })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "../reports/color-picker-mobile.png",
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "应用颜色" }).click();
  await expect(dialog).not.toBeVisible();
});
