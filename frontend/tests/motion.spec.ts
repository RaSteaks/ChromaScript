import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";
import type { MotionReply } from "../src/types";

// Use real HTTP/TSX compilation, sandboxed Player and local render jobs throughout.
async function setup(page: Page, template = "title") {
  const session = await (await page.request.get("/api/session")).json();
  async function call<T>(name: string, args: unknown): Promise<T> {
    const reply = await page.request.post("/api/operations/" + name, {
      headers: { Authorization: "Bearer " + session.token },
      data: args,
    });
    expect(reply.ok(), await reply.text()).toBe(true);
    return reply.json();
  }
  const { project } = await call<{ project: { id: string } }>(
    "create_project",
    { name: "动画网页验证 " + Date.now() },
  );
  const source = await call<MotionReply>("create_motion_scene", {
    project_id: project.id,
    template_id: template,
  });
  const saved = await call<MotionReply>("update_motion_scene", {
    scene_id: source.record.id,
    expected_revision_id: source.revision_id,
    spec: {
      ...source.scene,
      width: 320,
      height: 180,
      frames: 12,
      fps: { numerator: 30000, denominator: 1001 },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await page.getByLabel("动画项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("已保存的动画", { exact: true })
    .selectOption(saved.record.id);
  await expect(page.locator("iframe[title='动画画面']")).toBeVisible({
    timeout: 20000,
  });
  return { call, saved, project };
}

async function editCode(page: Page, code: string) {
  const editor = page.getByRole("textbox", {
    name: "动画 TSX 代码",
    exact: true,
  });
  await editor.fill(code);
}

test("Remotion editing, seeking, recovery and actual local video export", async ({
  page,
}) => {
  test.setTimeout(120000);
  const { saved, call } = await setup(page);
  const preview = page.getByRole("region", { name: "动画实时预览" });
  const frame = page.frameLocator("iframe[title='动画画面']");
  await expect(frame.getByText("让代码动起来", { exact: true })).toBeVisible();
  await page.getByLabel("动画时间线", { exact: true }).fill("7");
  await expect(preview).toContainText("7 / 11");
  await page.getByRole("button", { name: "播放动画", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "暂停动画", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "暂停动画", exact: true }).click();
  const originalPicture = await page
    .locator("iframe[title='动画画面']")
    .getAttribute("srcdoc");
  await editCode(
    page,
    "export default function Broken() { throw new Error('runtime-check'); }",
  );
  await expect(preview.getByRole("alert")).toContainText("runtime-check", {
    timeout: 20000,
  });
  await expect(page.locator("iframe[title='动画画面']")).toHaveAttribute(
    "srcdoc",
    originalPicture!,
  );
  await editCode(page, "export default function {");
  await expect(preview.getByRole("alert")).toContainText("Expected", {
    timeout: 20000,
  });
  await expect(page.locator("iframe[title='动画画面']")).toHaveAttribute(
    "srcdoc",
    originalPicture!,
  );
  const revised = saved.scene.code.replace("让代码动起来", "我的第一段动画");
  await editCode(page, revised);
  await expect(
    page
      .frameLocator("iframe[title='动画画面']")
      .getByText("我的第一段动画", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await expect(
    page.getByRole("button", { name: "导出 MP4", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await expect(
    page.getByRole("textbox", { name: "动画 TSX 代码" }),
  ).toContainText("我的第一段动画");
  // Save failure retains source and permits a deliberate retry.
  await page.route("**/api/operations/update_motion_scene", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { code: "unavailable", message: "保存服务暂不可用" } },
    }),
  );
  await page.getByRole("button", { name: "保存动画", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "保存服务暂不可用" }),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "动画 TSX 代码" }),
  ).toContainText("我的第一段动画");
  await page.unroute("**/api/operations/update_motion_scene");
  await page.getByRole("button", { name: "保存动画", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "导出 MP4", exact: true }),
  ).toBeEnabled();
  const destination = "/private/tmp/chromascript-motion-selected-" + Date.now();
  mkdirSync(destination, { recursive: true });
  await page.route("**/api/operations/choose_export_directory", (route) =>
    route.fulfill({ json: { cancelled: false, directory: destination } }),
  );
  await page.getByRole("button", { name: "选择文件夹", exact: true }).click();
  await expect(page.getByLabel("导出位置", { exact: true })).toHaveValue(
    destination,
  );
  await expect(
    page.getByRole("button", { name: "打开导出文件夹", exact: true }),
  ).not.toBeVisible();
  await page.getByRole("button", { name: "导出 MP4", exact: true }).click();
  const results = page.getByRole("region", {
    name: "动画渲染成果",
    exact: true,
  });
  await expect(
    results.getByRole("link", { name: "下载 MP4", exact: true }),
  ).toBeVisible({ timeout: 60000 });
  await expect(results).toContainText("已完成");
  let opened = "";
  await page.route("**/api/operations/open_export_directory", (route) => {
    opened = route.request().postDataJSON().directory;
    return route.fulfill({ json: { directory: opened } });
  });
  await results
    .getByRole("button", { name: "打开导出文件夹", exact: true })
    .click();
  await expect.poll(() => opened.startsWith(destination + "/")).toBe(true);
  const href = await results
    .getByRole("link", { name: "下载验证报告", exact: true })
    .getAttribute("href");
  const report = await (await page.request.get(href!)).json();
  expect(report.verification.passed).toBe(true);
  expect(report.verification.verified_frame_rate).toBe("30000/1001");
  expect(report.verification.scene_linear_master).toBe(false);
  const newest = await call<MotionReply>("get_motion_scene", {
    scene_id: saved.record.id,
  });
  expect(newest.scene.code).toBe(revised);
});

test("Three.js preview, revision conflict, keyboard and narrow accessibility", async ({
  page,
}) => {
  const { saved, call } = await setup(page, "three");
  const live = page.frameLocator("iframe[title='动画画面']");
  await expect(live.locator("canvas")).toBeVisible();
  await page.getByLabel("动画时间线", { exact: true }).fill("8");
  await expect(
    page.getByRole("region", { name: "动画实时预览" }),
  ).toContainText("8 / 11");
  await page.getByLabel("动画名称", { exact: true }).fill("本地未保存名称");
  await call("update_motion_scene", {
    scene_id: saved.record.id,
    expected_revision_id: saved.revision_id,
    spec: { ...saved.scene, name: "另一客户端名称" },
  });
  await page.getByRole("button", { name: "保存动画", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "载入最新版本", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("动画名称", { exact: true })).toHaveValue(
    "本地未保存名称",
  );
  await page.getByRole("button", { name: "载入最新版本", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "保留当前动画草稿" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(page.getByLabel("动画名称", { exact: true })).toHaveValue(
    "本地未保存名称",
  );
  const editor = page.getByRole("textbox", { name: "动画 TSX 代码" });
  await editor.focus();
  await page.keyboard.press("Tab");
  await expect(editor).not.toBeFocused();
  await page.getByRole("button", { name: "载入最新版本", exact: true }).click();
  await page
    .getByRole("button", { name: "丢弃修改并继续", exact: true })
    .click();
  await expect(page.getByLabel("动画名称", { exact: true })).toHaveValue(
    "另一客户端名称",
  );
  // Capture the committed picture rather than an in-progress hidden candidate.
  await expect(page.locator(".motion-preview-status")).toHaveText(
    "当前草稿 · 网页 sRGB 预览",
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect
    .poll(async () => {
      const canvas = await page
        .frameLocator("iframe[title='动画画面']")
        .locator("canvas")
        .boundingBox();
      const picture = await page
        .locator("iframe[title='动画画面']")
        .boundingBox();
      return canvas && picture ? Math.abs(canvas.width - picture.width) : 100;
    })
    .toBeLessThan(2);
  mkdirSync("../reports", { recursive: true });
  // DOM resize precedes the GPU compositor. Wait for two frame paints before
  // recording pixels, especially after the candidate iframe changes stacking.
  await page
    .frameLocator("iframe[title='动画画面']")
    .locator("body")
    .evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
  await page.screenshot({
    path: "../reports/motion-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(
    page.getByRole("button", { name: "播放动画", exact: true }),
  ).toBeEnabled();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByLabel("帧率", { exact: true }).selectOption("24/1");
  await expect(page.getByLabel("帧率", { exact: true })).toHaveValue("24/1");
  await expect(page.locator(".motion-preview-status")).toHaveText(
    "当前草稿 · 网页 sRGB 预览",
  );
  await page.screenshot({
    path: "../reports/motion-mobile.png",
    fullPage: true,
  });
});

test("animation accessibility", async ({ page }) => {
  await setup(page);
  const report = await new AxeBuilder({ page }).exclude("iframe").analyze();
  expect(report.violations).toEqual([]);
});

async function recoveryProject(page: Page) {
  const session = await (await page.request.get("/api/session")).json();
  const response = await page.request.post("/api/operations/create_project", {
    headers: { Authorization: "Bearer " + session.token },
    data: { name: "动画加载恢复 " + Date.now() },
  });
  expect(response.ok()).toBe(true);
  return (await response.json()).project as { id: string; name: string };
}

for (const legacyStatus of [404, 405]) {
  test(`legacy animation endpoints (${legacyStatus}) preserve projects and recover after reconnect`, async ({
    page,
  }) => {
    const project = await recoveryProject(page);
    // StaticFiles on an old daemon returns 405 with a string detail; a router
    // without the static fallback may return 404 for the same missing operation.
    for (const operation of [
      "list_motion_templates",
      "get_motion_capabilities",
    ]) {
      await page.route("**/api/operations/" + operation, (route) =>
        route.fulfill({
          status: legacyStatus,
          json: {
            detail: legacyStatus === 405 ? "Method Not Allowed" : "Not Found",
          },
        }),
      );
    }
    await page.goto("/");
    await page.getByRole("button", { name: "动画代码", exact: true }).click();
    await expect(page.getByLabel("动画项目", { exact: true })).toHaveValue(
      project.id,
    );
    await expect(
      page.getByRole("alert").filter({ hasText: "请重启 ChromaScript 服务" }),
    ).toBeVisible();
    await expect(
      page.getByText("先创建一个项目，再开始制作动画。", { exact: true }),
    ).not.toBeVisible();
    for (const operation of [
      "list_motion_templates",
      "get_motion_capabilities",
    ]) {
      await page.unroute("**/api/operations/" + operation);
    }
    await page
      .getByRole("button", { name: "刷新动画列表与连接", exact: true })
      .click();
    await expect(
      page.getByRole("alert").filter({ hasText: "请重启 ChromaScript 服务" }),
    ).not.toBeVisible();
    mkdirSync("../reports", { recursive: true });
    await page.screenshot({
      path: `../reports/motion-recovery-${legacyStatus}.png`,
    });
    await page
      .getByRole("button", { name: "新建文字动画", exact: true })
      .click();
    await expect(
      page.getByRole("textbox", { name: "动画 TSX 代码", exact: true }),
    ).toBeVisible();
    await expect(page.locator(".motion-preview-status")).toHaveText(
      "当前草稿 · 网页 sRGB 预览",
      { timeout: 20000 },
    );
  });
}

test("project discovery loading and failure never masquerade as an empty workspace", async ({
  page,
}) => {
  const project = await recoveryProject(page);
  let fail = false;
  let release: () => void = () => {};
  const responseGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/operations/list_projects", async (route) => {
    if (!fail) return route.continue();
    await responseGate;
    return route.fulfill({
      status: 503,
      json: { error: { code: "unavailable", message: "项目读取测试失败" } },
    });
  });
  await page.goto("/");
  await expect(page.getByLabel("当前项目", { exact: true })).toHaveValue(
    project.id,
  );
  fail = true;
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  try {
    await expect(
      page.getByText("正在读取动画项目…", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("先创建一个项目，再开始制作动画。", { exact: true }),
    ).not.toBeVisible();
  } finally {
    release();
  }
  await expect(
    page.getByRole("alert").filter({ hasText: "项目读取测试失败" }),
  ).toBeVisible();
  await expect(page.getByLabel("动画项目", { exact: true })).toContainText(
    "项目列表暂不可用，请重试",
  );
  await expect(
    page.getByText("先创建一个项目，再开始制作动画。", { exact: true }),
  ).not.toBeVisible();
  await page.unroute("**/api/operations/list_projects");
  await page
    .getByRole("button", { name: "刷新动画列表与连接", exact: true })
    .click();
  await expect(page.getByLabel("动画项目", { exact: true })).toHaveValue(
    project.id,
  );
  await expect(
    page.getByRole("alert").filter({ hasText: "项目读取测试失败" }),
  ).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "新建三维动画", exact: true }),
  ).toBeEnabled();
});
