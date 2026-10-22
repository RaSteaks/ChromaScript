import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { MotionReply, SceneReply } from "../src/types";

// Real storage, compilation and WebKit preview; export submission is intercepted so
// these UI tests never launch the production Chrome-based animation export worker.
async function setup(page: Page) {
  const session = await (await page.request.get("/api/session")).json();
  async function call<T>(operation: string, args: unknown): Promise<T> {
    const reply = await page.request.post(`/api/operations/${operation}`, {
      headers: { Authorization: `Bearer ${session.token}` },
      data: args,
    });
    expect(reply.ok(), await reply.text()).toBe(true);
    return reply.json();
  }
  const { project } = await call<{ project: { id: string } }>(
    "create_project",
    { name: `统一工作区 ${Date.now()}` },
  );
  const { project: other } = await call<{ project: { id: string } }>(
    "create_project",
    { name: `其他项目 ${Date.now()}` },
  );
  const wgsl = await call<SceneReply>("create_scene", {
    project_id: project.id,
    template_id: "gradient",
  });
  const float = await call<MotionReply>("create_motion_scene", {
    project_id: project.id,
    template_id: "radiance",
  });
  const react = await call<MotionReply>("create_motion_scene", {
    project_id: project.id,
    spec: {
      engine: "remotion-sdr-v1",
      name: "React 旧作品",
      width: 320,
      height: 180,
      frames: 24,
      fps: { numerator: 30000, denominator: 1001 },
      code: 'import React from "react"; import {useCurrentFrame} from "remotion"; export default function Animation() { return <div style={{color:"white",background:"#243746",height:"100%"}}>Frame {useCurrentFrame()}</div>; }',
      files: { "chapter.ts": "export const chapter = 1;" },
    },
  });
  await page.goto("/");
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await expect(page.getByLabel("当前作品", { exact: true })).toContainText(
    react.scene.name,
  );
  return { call, project, other, wgsl, float, react };
}
const work = (page: Page) => page.getByLabel("当前作品", { exact: true });
const wgslCode = (page: Page) =>
  page.getByRole("textbox", { name: "WGSL 场景代码", exact: true });
const motionCode = (page: Page) =>
  page.getByRole("textbox", { name: "动画 TypeScript 代码", exact: true });
async function stage(page: Page, step: "preview" | "delivery" | "jobs") {
  await page
    .getByRole("button", {
      name: {
        preview: "2. 编辑与预览",
        delivery: "3. 设置交付",
        jobs: "4. 任务与成果",
      }[step],
      exact: true,
    })
    .click();
}

test("one library opens WGSL, Three.js and React without rewriting existing revisions", async ({
  page,
}, info) => {
  const { call, wgsl, float, react } = await setup(page);
  await expect(
    page.getByRole("navigation", { name: "主要导航" }).getByRole("button"),
  ).toHaveCount(2);
  await expect(page.getByLabel("当前项目", { exact: true })).toHaveCount(1);
  await work(page).selectOption(`wgsl:${wgsl.record.id}`);
  await expect(wgslCode(page)).toHaveText(wgsl.scene.code, {
    useInnerText: true,
  });
  await expect(page.locator(".live-preview-image")).toBeVisible();
  await page.screenshot({
    path: info.outputPath("wgsl-workspace.png"),
    fullPage: true,
  });
  for (const scene of [float, react]) {
    await work(page).selectOption(`motion:${scene.record.id}`);
    await expect(motionCode(page)).toHaveText(scene.scene.code, {
      useInnerText: true,
    });
    await expect(page.locator('iframe[title="动画画面"]')).toBeVisible({
      timeout: 20000,
    });
    const timeline = page.getByLabel("动画时间线", { exact: true });
    await timeline.focus();
    await timeline.press("ArrowRight");
    await expect(timeline).toHaveValue("1");
    const saved = await call<MotionReply>("get_motion_scene", {
      scene_id: scene.record.id,
    });
    expect(saved.revision_id).toBe(scene.revision_id);
    expect(saved.scene).toEqual(scene.scene);
  }
  await page.screenshot({
    path: info.outputPath("react-workspace.png"),
    fullPage: true,
  });
  expect(
    (await call<SceneReply>("get_scene", { scene_id: wgsl.record.id }))
      .revision_id,
  ).toBe(wgsl.revision_id);
});

test("hidden drafts and timeline survive engine, stage and MCP navigation; project changes guard both", async ({
  page,
}) => {
  const { wgsl, react, other } = await setup(page);
  await work(page).selectOption(`wgsl:${wgsl.record.id}`);
  const shader = wgsl.scene.code + "\n// retained WGSL draft";
  await wgslCode(page).fill(shader);
  await page.getByLabel("当前帧", { exact: true }).fill("7");
  await work(page).selectOption(`motion:${react.record.id}`);
  const code = react.scene.code + "\n// retained React draft";
  await motionCode(page).fill(code);
  await expect(
    page.getByRole("button", { name: "保存作品", exact: true }),
  ).toBeEnabled();
  await page.locator("#motion-width").fill("640");
  await stage(page, "delivery");
  await expect(
    page.getByRole("button", { name: "导出动画", exact: true }),
  ).toBeDisabled();
  await stage(page, "jobs");
  await stage(page, "preview");
  await page.getByRole("button", { name: "MCP 接口", exact: true }).click();
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await expect(motionCode(page)).toHaveText(code, { useInnerText: true });
  await expect(page.locator("#motion-width")).toHaveValue("640");
  await work(page).selectOption(`wgsl:${wgsl.record.id}`);
  await expect(wgslCode(page)).toHaveText(shader, { useInnerText: true });
  await expect(page.getByLabel("当前帧", { exact: true })).toHaveValue("7");
  await page.getByRole("button", { name: "保存作品", exact: true }).click();
  // Only the hidden React draft is dirty now: it must still block a destructive project switch.
  await page.getByLabel("当前项目", { exact: true }).selectOption(other.id);
  await expect(
    page.getByRole("dialog", { name: "保留未保存修改" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await work(page).selectOption(`motion:${react.record.id}`);
  await expect(motionCode(page)).toHaveText(code, { useInnerText: true });
  await page.getByRole("button", { name: "保存作品", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "保存作品", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("当前项目", { exact: true }).selectOption(other.id);
  await expect(work(page)).not.toContainText(react.scene.name);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("same-engine replacement and a failed save preserve the original draft", async ({
  page,
}) => {
  const { react, float } = await setup(page);
  await work(page).selectOption(`motion:${react.record.id}`);
  const code = react.scene.code + "\n// never lose this edit";
  await motionCode(page).fill(code);
  await work(page).selectOption(`motion:${float.record.id}`);
  await expect(
    page.getByRole("dialog", { name: "保留当前动画草稿" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(motionCode(page)).toHaveText(code, { useInnerText: true });
  await page.route("**/api/operations/update_motion_scene", (route) =>
    route.fulfill({
      status: 409,
      json: { error: { code: "revision_conflict", message: "修订冲突测试" } },
    }),
  );
  await page.getByRole("button", { name: "保存作品", exact: true }).click();
  await expect(page.getByText("修订冲突测试", { exact: true })).toBeVisible();
  await expect(motionCode(page)).toHaveText(code, { useInnerText: true });
});

test("creation uses the shared controls for every engine", async ({ page }) => {
  await setup(page);
  for (const [engine, label] of [
    ["wgsl", "WGSL 场景代码"],
    ["three-float-v1", "动画 TypeScript 代码"],
    ["remotion-sdr-v1", "动画 TypeScript 代码"],
  ]) {
    await page.getByLabel("新建类型", { exact: true }).selectOption(engine);
    await page.getByRole("button", { name: "新建作品", exact: true }).click();
    await expect(
      page.getByRole("textbox", { name: label, exact: true }),
    ).toBeVisible();
    await expect(page.locator("#workflow-title")).toHaveText("编辑与预览");
    await expect(work(page)).not.toHaveValue("");
  }
});

test("unified delivery dispatches the saved source and keeps source-specific capabilities", async ({
  page,
}) => {
  const { react, float, wgsl } = await setup(page);
  for (const scene of [float, react]) {
    await work(page).selectOption(`motion:${scene.record.id}`);
    await stage(page, "delivery");
    const depth = page.getByLabel("EXR 母版精度", { exact: true });
    if (scene === float) await expect(depth).toBeVisible();
    else await expect(depth).toHaveCount(0);
    let submitted: Record<string, unknown> | undefined;
    await page.route(
      "**/api/operations/submit_motion_render",
      async (route) => {
        submitted = route.request().postDataJSON();
        await route.fulfill({
          status: 503,
          json: {
            error: { code: "test_export", message: "测试已拦截导出任务" },
          },
        });
      },
    );
    await page.getByRole("button", { name: "导出动画", exact: true }).click();
    await expect.poll(() => submitted?.scene_id).toBe(scene.record.id);
    expect(submitted?.expected_revision_id).toBe(scene.revision_id);
    await expect(
      page.getByText("测试已拦截导出任务", { exact: true }),
    ).toBeVisible();
    await page.unroute("**/api/operations/submit_motion_render");
  }
  await work(page).selectOption(`wgsl:${wgsl.record.id}`);
  await stage(page, "delivery");
  await expect(
    page.getByRole("button", { name: "导出动画", exact: true }),
  ).toBeHidden();
  await expect(page.getByRole("button", { name: /^提交交付 ·/ })).toBeVisible();
});

test("workspace accessibility and responsive editor layout", async ({
  page,
}, info) => {
  const { wgsl, react } = await setup(page);
  for (const [id, label] of [
    [`wgsl:${wgsl.record.id}`, "wgsl"],
    [`motion:${react.record.id}`, "react"],
  ]) {
    await work(page).selectOption(id);
    await expect(page.locator("#workflow-title")).toHaveText("编辑与预览");
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      const results = await new AxeBuilder({ page })
        .exclude("iframe")
        .analyze();
      expect(results.violations).toEqual([]);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: info.outputPath(`${label}-${width}.png`),
        fullPage: true,
      });
    }
  }
});

test("a failed animation open remains actionable while WGSL retains its draft", async ({
  page,
}) => {
  const { wgsl, react } = await setup(page);
  await work(page).selectOption(`wgsl:${wgsl.record.id}`);
  const shader = wgsl.scene.code + "\n// keep on failed open";
  await wgslCode(page).fill(shader);
  await page.route("**/api/operations/get_motion_scene", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { code: "unavailable", message: "读取动画失败，可重试" } },
    }),
  );
  await work(page).selectOption(`motion:${react.record.id}`);
  await expect(
    page.getByRole("alert").filter({ hasText: "读取动画失败，可重试" }),
  ).toBeVisible();
  await expect(wgslCode(page)).toHaveText(shader, { useInnerText: true });
  await page.unroute("**/api/operations/get_motion_scene");
  await work(page).selectOption(`motion:${react.record.id}`);
  await expect(motionCode(page)).toBeVisible();
  await work(page).selectOption(`wgsl:${wgsl.record.id}`);
  await expect(wgslCode(page)).toHaveText(shader, { useInnerText: true });
});

test("successful animation submission enters the shared results stage with the frozen revision", async ({
  page,
}) => {
  const { project, react } = await setup(page);
  await work(page).selectOption(`motion:${react.record.id}`);
  const job = {
    id: "test-motion-job",
    kind: "motion",
    scene_id: react.record.id,
    project_id: project.id,
    revision_id: react.revision_id,
    status: "queued",
    stage: "queued",
    progress: 0,
    completed_frames: 0,
    settings: { ...react.scene, samples: 1 },
    artifacts: [],
    error: null,
  };
  // Test the acknowledgement/navigation contract without launching an export browser.
  await page.route("**/api/operations/submit_motion_render", async (route) => {
    expect(route.request().postDataJSON().expected_revision_id).toBe(
      react.revision_id,
    );
    await route.fulfill({ json: { job } });
  });
  await page.route("**/api/operations/get_job", (route) =>
    route.fulfill({ json: { job, artifacts: [] } }),
  );
  await page.route("**/api/operations/list_jobs", (route) =>
    route.fulfill({ json: { items: [job], total: 1 } }),
  );
  await stage(page, "delivery");
  await page.getByRole("button", { name: "导出动画", exact: true }).click();
  await expect(page.locator("#workflow-title")).toHaveText("任务与成果");
  await expect(
    page.getByRole("progressbar", { name: "动画导出进度" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "取消渲染", exact: true }),
  ).toBeVisible();
  await stage(page, "preview");
  await expect(motionCode(page)).toHaveText(react.scene.code, {
    useInnerText: true,
  });
});

test("pagination pins the selected animation and refresh preserves its unsaved source", async ({
  page,
}) => {
  const { call, project, react } = await setup(page);
  for (let index = 0; index < 21; index++)
    await call("create_motion_scene", {
      project_id: project.id,
      engine: "remotion-sdr-v1",
    });
  await page.getByRole("button", { name: "刷新作品列表", exact: true }).click();
  await page.getByRole("button", { name: "下一页动画", exact: true }).click();
  await expect(work(page)).toContainText(react.scene.name);
  await work(page).selectOption(`motion:${react.record.id}`);
  const code = react.scene.code + "\n// paging must not reload this";
  await motionCode(page).fill(code);
  await page.getByRole("button", { name: "上一页动画", exact: true }).click();
  await expect(work(page)).toHaveValue(`motion:${react.record.id}`);
  await page.getByRole("button", { name: "刷新作品列表", exact: true }).click();
  await expect(motionCode(page)).toHaveText(code, { useInnerText: true });
});
