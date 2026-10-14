import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";
import type { MotionReply } from "../src/types";

async function showExamples(page: Page) {
  const disclosure = page
    .locator("details")
    .filter({ has: page.getByText("示例模板（可选）", { exact: true }) });
  if ((await disclosure.getAttribute("open")) === null)
    await disclosure.locator("summary").click();
}

// Use real HTTP/TSX compilation, sandboxed float renderer and local render jobs throughout.
async function setup(page: Page, template = "radiance") {
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
  await showExamples(page);
  return { call, saved, project };
}

async function editCode(page: Page, code: string) {
  const editor = page.getByRole("textbox", {
    name: "动画 TypeScript 代码",
    exact: true,
  });
  await editor.fill(code);
}

for (const suspendedAudio of [false, true]) {
  test(`silent playback advances, loops and pauses (suspended audio: ${suspendedAudio})`, async ({
    page,
  }) => {
    if (suspendedAudio) {
      // Model a browser/device that leaves audio activation pending. A silent
      // composition must advance even when AudioContext.resume never resolves.
      await page.addInitScript(() => {
        AudioContext.prototype.resume = () => new Promise<void>(() => {});
      });
    }
    await setup(page);
    const probe = page.frameLocator("iframe[title='动画画面']").locator("body");
    await expect(probe).toHaveAttribute("data-frame", "0");
    const timeline = page.getByLabel("动画时间线", { exact: true });
    await page.getByRole("button", { name: "播放动画", exact: true }).click();
    // Verify rendered composition time, not just the optimistic play event.
    await expect(probe).not.toHaveAttribute("data-frame", "0");
    await page.getByRole("button", { name: "暂停动画", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "播放动画", exact: true }),
    ).toBeEnabled();
    const paused = await timeline.inputValue();
    await expect(probe).toHaveAttribute("data-frame", paused);
    await page.waitForTimeout(250);
    await expect(timeline).toHaveValue(paused);
    await expect(probe).toHaveAttribute("data-frame", paused);

    await timeline.fill("11");
    await expect(probe).toHaveAttribute("data-frame", "11");
    await page.getByRole("button", { name: "播放动画", exact: true }).click();
    await expect(probe).not.toHaveAttribute("data-frame", "11");
    await page.getByRole("button", { name: "工作区", exact: true }).click();
    await page.getByRole("button", { name: "动画代码", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "播放动画", exact: true }),
    ).toBeEnabled();
  });
}

test("HDR editing, seeking, recovery and actual local video export", async ({
  page,
}) => {
  test.setTimeout(120000);
  const { saved, call } = await setup(page);
  const preview = page.getByRole("region", { name: "动画实时预览" });
  const frame = page.frameLocator("iframe[title='动画画面']");
  await expect(frame.locator("canvas")).toBeVisible();
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
  const revised = saved.scene.code.replace("12.0,3.0,0.4", "6.0,1.0,0.2");
  await editCode(page, revised);
  await expect(page.locator(".motion-preview-status")).toHaveText(
    "当前草稿 · 网页 sRGB 预览",
    { timeout: 20000 },
  );
  await expect(page.locator("iframe[title='动画画面']")).not.toHaveAttribute(
    "srcdoc",
    originalPicture!,
  );
  await expect(
    page.getByRole("button", { name: "导出动画", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await expect(
    page.getByRole("textbox", { name: "动画 TypeScript 代码" }),
  ).toContainText("6.0,1.0,0.2");
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
    page.getByRole("textbox", { name: "动画 TypeScript 代码" }),
  ).toContainText("6.0,1.0,0.2");
  await page.unroute("**/api/operations/update_motion_scene");
  await page.getByRole("button", { name: "保存动画", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "导出动画", exact: true }),
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
  await page.getByRole("button", { name: "导出动画", exact: true }).click();
  const results = page.getByRole("region", {
    name: "动画渲染成果",
    exact: true,
  });
  await expect(
    results.getByRole("link", { name: "下载视频 MP4", exact: true }),
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
  expect(report.verification.scene_linear_master).toBe(true);
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
  // Empty scene coverage stays transparent in the display copy, just as in EXR.
  const corner = await live.locator("body").evaluate(() => {
    const bridge = (
      window as unknown as {
        __chromaFloat: {
          sampleDisplay: (frame: number, x: number, y: number) => number[];
        };
      }
    ).__chromaFloat;
    return bridge.sampleDisplay(0, 0, 0);
  });
  expect(corner).toEqual([0, 0, 0, 0]);

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
  const editor = page.getByRole("textbox", { name: "动画 TypeScript 代码" });
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
    await showExamples(page);
    await page
      .getByRole("button", { name: "新建HDR 光能渐变", exact: true })
      .click();
    await expect(
      page.getByRole("textbox", { name: "动画 TypeScript 代码", exact: true }),
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
  await showExamples(page);
  await expect(
    page.getByRole("button", { name: "新建HDR 发光几何", exact: true }),
  ).toBeEnabled();
});

test("HDR delivery choices preserve the sRGB preview and export half-float masters", async ({
  page,
}) => {
  await setup(page);
  const picture = page.locator("iframe[title='动画画面']");
  const source = await picture.getAttribute("srcdoc");
  await page.getByLabel("EXR 母版精度", { exact: true }).selectOption("16");
  await page.getByLabel("视频输出", { exact: true }).selectOption("none");
  await page.getByLabel("动画时间线", { exact: true }).fill("7");
  await expect(picture).toHaveAttribute("srcdoc", source!);
  await expect(
    page.frameLocator("iframe[title='动画画面']").locator("body"),
  ).toHaveAttribute("data-frame", "7");
  await page.getByRole("button", { name: "导出动画", exact: true }).click();
  const results = page.getByRole("region", {
    name: "动画渲染成果",
    exact: true,
  });
  await expect(
    results.getByRole("link", { name: "下载 ACEScg 母版", exact: true }),
  ).toBeVisible({ timeout: 60000 });
  await expect(
    results.getByRole("link", { name: "下载视频 MP4", exact: true }),
  ).not.toBeVisible();
  await expect(
    results.getByRole("img", { name: "导出母版的 sRGB 预览" }),
  ).toBeVisible();
  const href = await results
    .getByRole("link", { name: "下载验证报告", exact: true })
    .getAttribute("href");
  const report = await (await page.request.get(href!)).json();
  expect(report.verification.master_bit_depth).toBe(16);
  expect(report.verification.scene_linear_master).toBe(true);
});

// Compare actual GPU display pixels against the CPU OCIO path used for exported
// thumbnails and native MCP images, including extended radiance and negative RGB.
test("GPU sRGB preview matches the CPU ACES view for HDR and negative samples", async ({
  page,
}) => {
  const { saved, call } = await setup(page);
  const code = saved.scene.code
    .replace(
      "mix(vec3(0.01,0.025,0.06),vec3(12.0,3.0,0.4),glow)",
      "p.x < 0.2 ? vec3(0.001) : p.x < 0.4 ? vec3(0.18) : p.x < 0.6 ? vec3(4.125,0.1234567,-0.25) : p.x < 0.8 ? vec3(12.0,3.0,0.4) : vec3(64.0)",
    )
    .replace("radiance*(0.2+0.8*p.y)", "radiance");
  const previous = await page
    .locator("iframe[title='动画画面']")
    .getAttribute("srcdoc");
  await editCode(page, code);
  await expect(page.locator("iframe[title='动画画面']")).not.toHaveAttribute(
    "srcdoc",
    previous!,
  );
  await expect(page.locator(".motion-preview-status")).toHaveText(
    "当前草稿 · 网页 sRGB 预览",
  );
  const result = await call<{ images: { data: string }[] }>(
    "render_motion_frame",
    { spec: { ...saved.scene, code }, frame: 0 },
  );
  const xs = [20, 80, 160, 250, 300];
  const expected = await page.evaluate(
    async ({ data, xs }) => {
      const image = new Image();
      image.src = "data:image/png;base64," + data;
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = image.width;
      canvas.height = image.height;
      const context = canvas.getContext("2d")!;
      context.drawImage(image, 0, 0);
      return xs.map((x) =>
        Array.from(context.getImageData(x, image.height - 1 - 90, 1, 1).data),
      );
    },
    { data: result.images[0].data, xs },
  );
  const actual = await page
    .frameLocator("iframe[title='动画画面']")
    .locator("body")
    .evaluate((_, xs) => {
      const bridge = (
        window as unknown as {
          __chromaFloat: {
            sampleDisplay: (frame: number, x: number, y: number) => number[];
          };
        }
      ).__chromaFloat;
      return xs.map((x) => bridge.sampleDisplay(0, x, 90));
    }, xs);
  for (let i = 0; i < xs.length; i++)
    for (let c = 0; c < 4; c++)
      expect(Math.abs(actual[i][c] - expected[i][c])).toBeLessThanOrEqual(2);
});

test("unsupported float rendering reports an error instead of certifying an SDR fallback", async ({
  page,
}) => {
  const session = await (await page.request.get("/api/session")).json();
  const headers = { Authorization: "Bearer " + session.token };
  const project = await (
    await page.request.post("/api/operations/create_project", {
      headers,
      data: { name: "Float unavailable" },
    })
  ).json();
  const templates = await (
    await page.request.post("/api/operations/list_motion_templates", {
      headers,
      data: {},
    })
  ).json();
  const spec = templates.items.find(
    (item: { id: string }) => item.id === "radiance",
  ).spec;
  // Fault injection lives inside the isolated scene module, before renderer
  // creation. Browser init scripts do not reliably run before srcdoc scripts.
  spec.code =
    `const getExtension = WebGL2RenderingContext.prototype.getExtension;
WebGL2RenderingContext.prototype.getExtension = function(name) {
  return name === "EXT_color_buffer_float" ? null : getExtension.call(this,name);
};\n` + spec.code;
  await page.request.post("/api/operations/create_motion_scene", {
    headers,
    data: { project_id: project.project.id, spec },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await page
    .getByLabel("动画项目", { exact: true })
    .selectOption(project.project.id);
  const scene = await (
    await page.request.post("/api/operations/list_motion_scenes", {
      headers,
      data: { project_id: project.project.id },
    })
  ).json();
  await page
    .getByLabel("已保存的动画", { exact: true })
    .selectOption(scene.items[0].id);
  await expect(
    page.getByRole("region", { name: "动画实时预览" }).getByRole("alert"),
  ).toContainText("GPU 不支持浮点渲染目标");
  await expect(
    page.getByRole("button", { name: "播放动画", exact: true }),
  ).toBeDisabled();
  await expect(page.locator("iframe[title='动画画面']")).toHaveCount(0);
});

for (const template of ["title", "sdr-three"]) {
  test(`SDR template ${template} opens, plays, preserves its engine and exports`, async ({
    page,
  }) => {
    test.setTimeout(120000);
    const { saved, call } = await setup(page, template);
    await expect(
      page.getByRole("button", { name: "新建SDR 文字与图形", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "新建SDR 三维旋转几何", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "新建HDR 光能渐变", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "新建HDR 发光几何", exact: true }),
    ).toBeVisible();
    const live = page.frameLocator("iframe[title='动画画面']");
    if (template === "title")
      await expect(
        live.getByText("让代码动起来", { exact: true }),
      ).toBeVisible();
    else await expect(live.locator("canvas")).toBeVisible();
    await expect(page.getByLabel("EXR 母版精度", { exact: true })).toHaveCount(
      0,
    );
    const video = page.getByLabel("视频输出", { exact: true });
    await expect(video).toHaveValue("srgb");
    await expect(video.locator("option")).toHaveCount(2);
    const timeline = page.getByLabel("动画时间线", { exact: true });
    await page.getByRole("button", { name: "播放动画", exact: true }).click();
    await expect(timeline).not.toHaveValue("0");
    await page.getByRole("button", { name: "暂停动画", exact: true }).click();
    await timeline.fill("7");
    await expect(
      page.getByRole("region", { name: "动画实时预览" }),
    ).toContainText("7 / 11");
    await page.getByLabel("动画名称", { exact: true }).fill("SDR preserved");
    await page.getByRole("button", { name: "保存动画", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "保存动画", exact: true }),
    ).toBeDisabled();
    const updated = await call<MotionReply>("get_motion_scene", {
      scene_id: saved.record.id,
    });
    expect(updated.scene.engine).toBe("remotion-sdr-v1");
    expect(updated.scene.code).toBe(saved.scene.code);
    await video.selectOption("rec709");
    await expect(video).toHaveValue("rec709");
    await page.getByRole("button", { name: "导出动画", exact: true }).click();
    const results = page.getByRole("region", {
      name: "动画渲染成果",
      exact: true,
    });
    await expect(
      results.getByRole("link", { name: "下载视频 MP4", exact: true }),
    ).toBeVisible({ timeout: 60000 });
    await expect(
      results.getByRole("link", { name: "下载 ACEScg 母版", exact: true }),
    ).toHaveCount(0);
    const url = await results
      .getByRole("link", { name: "下载验证报告", exact: true })
      .getAttribute("href");
    const report = await (await page.request.get(url!)).json();
    expect(report.verification.scene_linear_master).toBe(false);
    expect(report.probe.streams[0].color_transfer).toBe("bt709");
    // A source-family switch refreshes output constraints, not just its labels.
    await showExamples(page);
    await page
      .getByRole("button", { name: "新建HDR 光能渐变", exact: true })
      .click();
    await expect(
      page.getByLabel("EXR 母版精度", { exact: true }),
    ).toBeVisible();
    await expect(video).toHaveValue("pq");
    await page
      .getByLabel("已保存的动画", { exact: true })
      .selectOption(saved.record.id);
    await expect(video).toHaveValue("srgb");
    await expect(page.getByLabel("EXR 母版精度", { exact: true })).toHaveCount(
      0,
    );
  });
}

for (const engine of ["remotion-sdr-v1", "three-float-v1"]) {
  test(`blank animation ${engine} starts without opening examples and guards drafts`, async ({
    page,
  }) => {
    const project = await recoveryProject(page);
    await page.goto("/");
    await page.getByRole("button", { name: "动画代码", exact: true }).click();
    await page.getByLabel("动画项目", { exact: true }).selectOption(project.id);
    await expect(
      page.getByRole("button", { name: "新建HDR 光能渐变", exact: true }),
    ).not.toBeVisible();
    await page.getByLabel("空白动画类型", { exact: true }).selectOption(engine);
    const created = page.waitForResponse(
      (r) =>
        r.url().endsWith("/create_motion_scene") &&
        r.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "新建空白动画", exact: true })
      .click();
    const response = await created;
    expect(response.request().postDataJSON().template_id).toBeNull();
    const value = await response.json();
    expect(value.scene.engine).toBe(engine);
    await expect(page.getByLabel("动画名称", { exact: true })).toHaveValue(
      engine === "remotion-sdr-v1" ? "空白 SDR 动画" : "空白浮点动画",
    );
    await expect(page.locator(".motion-preview-status")).toHaveText(
      "当前草稿 · 网页 sRGB 预览",
      { timeout: 20000 },
    );
    await page.getByLabel("动画名称", { exact: true }).fill("未保存的空白动画");
    await page
      .getByRole("button", { name: "新建空白动画", exact: true })
      .click();
    await expect(
      page.getByRole("dialog", { name: "保留当前动画草稿" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "继续编辑", exact: true }).click();
    await expect(page.getByLabel("动画名称", { exact: true })).toHaveValue(
      "未保存的空白动画",
    );
  });
}
