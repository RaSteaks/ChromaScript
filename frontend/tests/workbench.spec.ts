import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";
import type { Artifact, Job, SceneReply } from "../src/types";

// Exercise the same visible stage controls a user follows; no hidden-panel interactions.
async function stage(
  page: Page,
  name: "scene" | "preview" | "delivery" | "jobs",
) {
  const labels = {
    scene: "1. 选择场景",
    preview: "2. 编辑与预览",
    delivery: "3. 设置交付",
    jobs: "4. 任务与成果",
  };
  await page
    .getByRole("navigation", { name: "创作流程" })
    .getByRole("button", { name: labels[name], exact: true })
    .click();
  await expect(page.locator("#workflow-title")).toHaveText(
    labels[name].slice(3),
  );
}

test("selected job previews survive queue pagination and retain scene ownership", async ({
  page,
}) => {
  const session = await (await page.request.get("/api/session")).json();
  async function call<T>(operation: string, args: unknown): Promise<T> {
    const response = await page.request.post("/api/operations/" + operation, {
      headers: { Authorization: "Bearer " + session.token },
      data: args,
    });
    expect(response.ok()).toBe(true);
    return response.json();
  }
  const { project } = await call<{ project: { id: string } }>(
    "create_project",
    {
      name: "队列分页验证 " + Date.now(),
    },
  );
  const first = await call<SceneReply>("create_scene", {
    project_id: project.id,
    template_id: "gradient",
  });
  const other = await call<SceneReply>("create_scene", {
    project_id: project.id,
    spec: { ...first.scene, name: "另一分页场景" },
  });
  const selected = await call<{ job: Job }>("render_preview", {
    scene_id: first.record.id,
    settings: {
      width: 32,
      height: 32,
      frames: 2,
      samples: 1,
      fps: { numerator: 24, denominator: 1 },
    },
  });
  const another = await call<{ job: Job }>("render_frame", {
    scene_id: other.record.id,
    width: 32,
    height: 32,
    samples: 1,
  });
  const completed: { job: Job; artifacts: Artifact[] }[] = [];
  for (const { job } of [selected, another]) {
    await expect
      .poll(
        async () => {
          const reply = await call<(typeof completed)[number]>("get_job", {
            job_id: job.id,
          });
          if (reply.job.status === "completed") completed.push(reply);
          return reply.job.status;
        },
        { timeout: 20000 },
      )
      .toBe("completed");
  }
  // Only queue filler rows are synthetic; selected jobs, artifacts, GPU frames
  // and the review movie come from the real service in this isolated project.
  const queue = [
    completed[0].job,
    ...Array.from({ length: 7 }, (_, i) => ({
      ...completed[1].job,
      id: `pagination-filler-${i}`,
      status: "cancelled",
    })),
    completed[1].job,
  ];
  await page.route("**/api/operations/list_jobs", (route) => {
    const args = route.request().postDataJSON();
    if (args.project_id !== project.id) return route.continue();
    return route.fulfill({
      json: {
        items: queue.slice(args.offset, args.offset + args.limit),
        total: queue.length,
      },
    });
  });
  await page.goto("/");
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByRole("button", { name: first.scene.name, exact: true })
    .click();
  await stage(page, "jobs");
  const results = page.getByRole("region", { name: "任务成果" });
  const previewLink = results.getByRole("button", { name: "查看画面预览" });
  await expect(previewLink).toBeVisible();
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.locator(".jobs .job-identity")).toHaveCount(1);
  await expect(results).toContainText(selected.job.id.slice(0, 8));
  await expect(results).toContainText("动画预览 · 已完成 · 32 × 32");
  await expect(previewLink).toBeVisible();
  await previewLink.click();
  await page.getByRole("button", { name: "关键帧", exact: true }).click();
  const image = completed[0].artifacts.find((a) => a.kind === "preview-image")!;
  await expect(page.locator(".preview-stage img")).toHaveAttribute(
    "src",
    "/api/artifacts/" + image.id,
  );
  // Sampling uses decoded image dimensions; a newly mounted image may still load.
  await expect
    .poll(() =>
      page
        .locator(".preview-stage img")
        .evaluate((img: HTMLImageElement) => img.naturalWidth),
    )
    .toBe(32);
  await page
    .getByRole("button", { name: "在预览中取样，按回车可取中心像素" })
    .press("Enter");
  await expect(page.getByText("像素 16, 16", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "运动预览", exact: true }).click();
  await expect(page.locator(".preview-stage video")).toBeVisible();
  await stage(page, "jobs");
  await page.locator(".jobs .job-identity").click();
  await expect(results).toContainText(another.job.id.slice(0, 8));
  await expect(results.locator(".artifact-strip")).toBeVisible();
  await expect(previewLink).toHaveCount(0);
  // A job can finish after its row leaves the page. The detail poll must still
  // publish its completed state and load results without returning to that page.
  queue[0] = { ...completed[0].job, status: "running", progress: 0.5 };
  await page.getByRole("button", { name: "上一页", exact: true }).click();
  await page
    .locator(".jobs .job-identity")
    .filter({ hasText: selected.job.id.slice(0, 8) })
    .click();
  await expect(
    results.getByText("任务正在执行", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.locator(".jobs .job-identity")).toHaveCount(1);
  await expect(results).toContainText("动画预览 · 已完成 · 32 × 32");
  await expect(previewLink).toBeVisible();
});

test("project, scene, saved revision, real preview and keyboard pixel sampling", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".engine-state")).toContainText("Metal");
  await page.getByRole("button", { name: "新建项目", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "新建项目" });
  await dialog.getByRole("button", { name: "创建项目", exact: true }).click();
  await expect(dialog.getByText("请输入项目名称。")).toBeVisible();
  await page
    .getByLabel("项目名称", { exact: true })
    .fill("浏览器验证 " + Date.now());
  await dialog.getByRole("button", { name: "创建项目", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: /螺旋粒子/ }).click();
  await expect(
    page.getByRole("heading", { name: "螺旋粒子", exact: true }),
  ).toBeVisible();
  await page.getByLabel("曝光（档）", { exact: true }).fill("0.5");
  await expect(page.getByText("未保存", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "保存场景", exact: true }).click();
  await expect(page.getByText("保存场景已完成", { exact: true })).toBeVisible();
  // Shared canvas settings are edited beside the live preview.
  await stage(page, "preview");
  await page.getByLabel("宽度", { exact: true }).fill("64");
  await page.getByLabel("高度", { exact: true }).fill("64");
  await page.getByLabel("总帧数", { exact: true }).fill("2");
  await stage(page, "preview");
  await page.getByRole("button", { name: "渲染所选帧" }).click();
  const picture = page.locator(".preview-stage img");
  await expect(picture).toBeVisible({ timeout: 20000 });
  await expect
    .poll(() => picture.evaluate((img: HTMLImageElement) => img.naturalWidth))
    .toBeGreaterThan(0);
  const sampler = page.getByRole("button", {
    name: "在预览中取样，按回车可取中心像素",
  });
  await sampler.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("像素 32, 32", { exact: true })).toBeVisible();
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/workbench-desktop.png",
    fullPage: true,
  });
  await page.getByLabel("曝光（档）", { exact: true }).fill("0.7");
  await stage(page, "scene");
  await page.getByRole("button", { name: /广色域渐变/ }).click();
  await expect(
    page.getByRole("dialog", { name: "保留场景修改" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await stage(page, "preview");
  await expect(page.getByLabel("曝光（档）", { exact: true })).toHaveValue(
    "0.7",
  );
});

test("linked format choices preserve compatible values and repair conflicts", async ({
  page,
}) => {
  await page.goto("/");
  await stage(page, "delivery");
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  const field = (name: string) => target.getByLabel(name, { exact: true });
  await expect(field("封装 / 图像格式")).toBeEnabled();
  await field("质量控制").selectOption("bitrate");
  await expect(field("目标码率（kb/s）")).toHaveValue("20000");
  await field("目标码率（kb/s）").fill("");
  await expect(field("目标码率（kb/s）")).toHaveAttribute(
    "aria-invalid",
    "true",
  );
  await expect(page.getByRole("button", { name: /^提交交付/ })).toBeDisabled();
  await field("目标码率（kb/s）").fill("12000");
  await expect(field("目标码率（kb/s）")).toHaveAttribute(
    "aria-invalid",
    "false",
  );
  // Native typeahead exercises a real keyboard change. macOS headless Chrome
  // does not route ArrowDown to its OS menu (also reproduced on plain HTML select).
  await field("封装 / 图像格式").focus();
  await page.keyboard.press("q");
  await page.keyboard.press("Enter");
  await expect(field("封装 / 图像格式")).toHaveValue("mov");
  await expect(field("视频 / 图像编码")).toBeEnabled();
  await field("视频 / 图像编码").selectOption("prores");
  await expect(field("编码档位")).toHaveValue("hq");
  await expect(field("位深")).toHaveValue("10");
  await expect(field("色度采样")).toHaveValue("422");
  await field("Alpha").selectOption("straight");
  await expect(field("编码档位")).toHaveValue("4444");
  await expect(field("色度采样")).toHaveValue("444");
  await field("编码档位").selectOption("proxy");
  await expect(field("Alpha")).toHaveValue("none");
  await expect(field("色度采样")).toHaveValue("422");
  await field("封装 / 图像格式").selectOption("mp4");
  await expect(field("视频 / 图像编码")).toHaveValue("h264");
  await expect(field("视频 / 图像编码").locator("option")).toHaveCount(2);
  await expect(field("Alpha")).toBeDisabled();
  await field("视频 / 图像编码").selectOption("hevc");
  await expect(field("位深")).toBeEnabled();
  await field("位深").selectOption("8");
  await expect(field("编码档位")).toHaveValue("main");
  // The active color contract exposes gamut and transfer as paired controls.
  await field("输出色彩空间").selectOption("Rec.2020");
  await expect(field("传递函数 / Gamma")).toHaveValue("st2084");
  await expect(field("位深")).toHaveValue("10");
  await expect(field("编码档位")).toHaveValue("main10");
  await field("传递函数 / Gamma").selectOption("hlg");
  await expect(field("输出色彩空间")).toHaveValue("Rec.2020");
  await field("位深").selectOption("8");
  await expect(field("输出色彩空间")).toHaveValue("Rec.709");
  // SDR fallback must retain an exact native transfer tag instead of a Gamma 2.4 convention.
  await expect(field("传递函数 / Gamma")).toHaveValue("srgb");
  await field("传递函数 / Gamma").selectOption("srgb");
  await expect(field("输出色彩空间")).toHaveValue("Rec.709");
  await field("封装 / 图像格式").selectOption("exr");
  await expect(field("输出色彩空间")).toHaveValue("ACEScg");
  await expect(field("传递函数 / Gamma")).toHaveValue("linear");
  await expect(field("传递函数 / Gamma")).toBeDisabled();
  // Auto remains selected while the server resolves EXR to Full in the frozen output.
  await expect(field("数据级别")).toHaveValue("auto");
  await expect(target).toContainText("Auto 按当前格式使用 Full（全范围）");
  await field("位深").selectOption("32");
  await expect(field("位深")).toHaveValue("32");
  await field("图像压缩").selectOption("piz");
  await expect(field("图像压缩")).toHaveValue("piz");
});

test("MCP operation explorer uses the shared service", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".engine-state")).toContainText("Metal");
  await page.getByRole("button", { name: "MCP 接口", exact: true }).click();
  await page
    .getByLabel("操作", { exact: true })
    .selectOption("chromascript.list_templates");
  await page.getByRole("button", { name: "运行接口", exact: true }).click();
  await expect(page.locator("#operation-result")).toContainText("螺旋粒子");
  await expect(page.locator("pre").first()).toContainText("chromascript");
});

test("accessibility and responsive workstation", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".engine-state")).toContainText("Metal");
  // Every stage is a different layout, including long delivery forms and the result list.
  for (const name of ["scene", "preview", "delivery", "jobs"] as const) {
    await stage(page, name);
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(results.violations).toEqual([]);
    mkdirSync("../reports", { recursive: true });
    await page.screenshot({
      path: `../reports/workflow-${name}.png`,
      fullPage: true,
    });
  }
  await page.getByRole("button", { name: "MCP 接口", exact: true }).click();
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  for (const name of ["scene", "preview", "delivery", "jobs"] as const) {
    await stage(page, name);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  }
  await page.screenshot({
    path: "../reports/workbench-mobile.png",
    fullPage: true,
  });
  await stage(page, "scene");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.getByRole("button", { name: "新建项目", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "新建项目" });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "新建项目", exact: true }),
  ).toBeFocused();
});

test("offline feedback preserves local draft and reconnects", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".engine-state")).toContainText("Metal");
  await stage(page, "preview");
  await page.locator(".code-disclosure > summary").click();
  const editor = page.getByLabel("WGSL 场景代码");
  await expect(editor).toBeEnabled();
  {
    const old = await editor.inputValue();
    await editor.fill(old + "\n// local draft");
    await page.route("**/api/operations/list_jobs", (route) =>
      route.abort("failed"),
    );
    await expect(
      page.getByText("本地服务暂不可用，编辑内容已保留。"),
    ).toBeVisible({ timeout: 10000 });
    await expect(editor).toHaveValue(old + "\n// local draft");
    await page.unroute("**/api/operations/list_jobs");
    await page.getByRole("button", { name: "重新连接", exact: true }).click();
    await expect(
      page.getByText("本地服务暂不可用，编辑内容已保留。"),
    ).not.toBeVisible();
  }
});

test("image sequence export preserves destination on errors and copies all frames", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".engine-state")).toContainText("Metal");
  await stage(page, "preview");
  await expect(page.getByLabel("宽度", { exact: true })).toBeVisible();
  await page.getByLabel("宽度", { exact: true }).fill("64");
  await page.getByLabel("高度", { exact: true }).fill("64");
  await page.getByLabel("总帧数", { exact: true }).fill("2");
  await stage(page, "delivery");
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  await target
    .getByLabel("封装 / 图像格式", { exact: true })
    .selectOption("exr");
  await expect(
    target.getByLabel("视频 / 图像编码", { exact: true }),
  ).toHaveValue("exr");
  await page.getByRole("button", { name: /^提交交付/ }).click();
  await expect(page.locator("#workflow-title")).toHaveText("任务与成果");
  await page
    .getByRole("button", { name: /^导出序列 delivery-exr-/ })
    .click({ timeout: 20000 });
  const dialog = page.getByRole("dialog", { name: "导出图像序列" });
  const destination = dialog.getByLabel("导出目录", { exact: true });
  const original = await destination.inputValue();
  await destination.fill("");
  await dialog.getByRole("button", { name: "复制序列", exact: true }).click();
  await expect(dialog.getByText("请输入导出目录。")).toBeVisible();
  await destination.fill("relative/export");
  await dialog.getByRole("button", { name: "复制序列", exact: true }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(destination).toHaveValue("relative/export");
  await destination.fill(original);
  await dialog.getByRole("button", { name: "复制序列", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  // A delivery also exposes its automatic bundle beside a separate sequence export.
  await expect(
    page.locator(".export-result").filter({ hasText: original }),
  ).toBeVisible();
  // Native filesystem evidence confirms the visible action exported the entire sequence.
  const { readdirSync } = await import("node:fs");
  expect(readdirSync(original).sort()).toEqual([
    "00000000.exr",
    "00000001.exr",
    "sequence.json",
  ]);
});

test("multiple targets constrain frame rates and remain independent", async ({
  page,
}) => {
  await page.goto("/");
  await stage(page, "delivery");
  const first = page.getByRole("group", { name: "交付目标 1", exact: true });
  await expect(
    first.getByLabel("封装 / 图像格式", { exact: true }),
  ).toBeEnabled();
  await stage(page, "preview");
  await page.getByLabel("帧率", { exact: true }).selectOption("120/1");
  await stage(page, "delivery");
  await page.getByRole("button", { name: "添加交付目标", exact: true }).click();
  const second = page.getByRole("group", { name: "交付目标 2", exact: true });
  await expect(
    second.getByLabel("封装 / 图像格式", { exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByText("交付设置重复，请调整或移除重复目标。"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^提交交付/ })).toBeDisabled();
  await second
    .getByLabel("封装 / 图像格式", { exact: true })
    .selectOption("mxf");
  await expect(
    second.getByLabel("视频 / 图像编码", { exact: true }),
  ).toHaveValue("dnxhr");
  await expect(
    page.getByRole("region", { name: "交付画面规格" }),
  ).toContainText("60 fps");
  await stage(page, "preview");
  await expect(page.getByLabel("帧率", { exact: true })).toHaveValue("60/1");
  await expect(
    page.getByLabel("帧率", { exact: true }).locator('option[value="120/1"]'),
  ).toHaveCount(0);
  await stage(page, "delivery");
  await expect(
    first.getByLabel("封装 / 图像格式", { exact: true }),
  ).toHaveValue("mp4");
  await page
    .getByRole("button", { name: "移除交付目标 2", exact: true })
    .click();
  await expect(second).not.toBeVisible();
  await expect(
    first.getByLabel("封装 / 图像格式", { exact: true }),
  ).toBeFocused();
});

test("failed and stale linked requests preserve the most recent selection", async ({
  page,
}) => {
  await page.goto("/");
  await stage(page, "delivery");
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  const container = target.getByLabel("封装 / 图像格式", { exact: true });
  await expect(container).toBeEnabled();
  await page.route("**/api/operations/get_delivery_options", async (route) => {
    if (route.request().postDataJSON().configuration?.container === "mov") {
      const response = await route.fetch();
      await new Promise((resolve) => setTimeout(resolve, 350));
      try {
        await route.fulfill({ response });
      } catch {
        /* The newer request aborts this native fetch. */
      }
    } else await route.continue();
  });
  await container.selectOption("mov");
  await container.selectOption("exr");
  await expect(
    target.getByLabel("视频 / 图像编码", { exact: true }),
  ).toHaveValue("exr");
  await page.waitForTimeout(450);
  await expect(container).toHaveValue("exr");
  await page.unroute("**/api/operations/get_delivery_options");
  let fail = true;
  await page.route("**/api/operations/get_delivery_options", (route) => {
    if (fail) {
      fail = false;
      return route.abort("failed");
    }
    return route.continue();
  });
  await container.selectOption("png");
  await expect(target.getByRole("alert")).toBeVisible();
  await expect(container).toHaveValue("png");
  await target
    .getByRole("button", { name: "重试联动选项", exact: true })
    .click();
  await expect(target.getByRole("alert")).not.toBeVisible();
  await expect(
    target.getByLabel("视频 / 图像编码", { exact: true }),
  ).toHaveValue("png");
});

test("workflow stages retain drafts and provide clear prerequisites", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".engine-state")).toContainText("Metal");
  await stage(page, "preview");
  await expect(page.locator("#workflow-title")).toBeFocused();
  await page.locator(".code-disclosure > summary").click();
  const editor = page.getByLabel("WGSL 场景代码");
  const original = await editor.inputValue();
  await editor.fill(original + "\n// workflow draft");
  await page.getByLabel("总帧数", { exact: true }).fill("17");
  await stage(page, "delivery");
  await expect(
    page.getByText("当前场景有未保存修改，请返回编辑并保存。"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^提交交付/ })).toBeDisabled();
  await expect(
    page.getByRole("region", { name: "交付画面规格" }),
  ).toContainText("17 帧");
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  await target
    .getByLabel("封装 / 图像格式", { exact: true })
    .selectOption("exr");
  await stage(page, "jobs");
  await expect(
    page.getByRole("dialog", { name: "保留场景修改" }),
  ).not.toBeVisible();
  await stage(page, "delivery");
  await expect(
    page.getByRole("region", { name: "交付画面规格" }),
  ).toContainText("17 帧");
  await expect(
    target.getByLabel("封装 / 图像格式", { exact: true }),
  ).toHaveValue("exr");
  await page.getByRole("button", { name: "返回保存场景", exact: true }).click();
  await expect(page.getByLabel("总帧数", { exact: true })).toHaveValue("17");
  await page.locator(".code-disclosure > summary").click();
  await expect(editor).toHaveValue(original + "\n// workflow draft");
  await page.getByRole("button", { name: "MCP 接口", exact: true }).click();
  await expect(page).toHaveTitle("MCP 接口 · ChromaScript");
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await expect(page).toHaveTitle("编辑与预览 · ChromaScript");
  await page.locator(".code-disclosure > summary").click();
  await expect(editor).toHaveValue(original + "\n// workflow draft");
});

test("empty workflow directs users to create a scene", async ({ page }) => {
  await page.route("**/api/operations/list_projects", (route) =>
    // Project discovery now follows the API's total across all bounded pages.
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.goto("/");
  await expect(page.locator(".engine-state")).toContainText("Metal");
  await expect(page.locator("#workflow-title")).toHaveText("选择场景");
  await stage(page, "delivery");
  await expect(
    page.getByRole("heading", { name: "先选择一个场景", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^提交交付/ })).toHaveCount(0);
  await page.getByRole("button", { name: "前往选择场景", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "新建项目", exact: true }),
  ).toBeVisible();
  await stage(page, "jobs");
  await expect(
    page.getByText("提交预览或交付后，可在这里查看进度、成果和恢复任务。"),
  ).toBeVisible();
});

test("failed delivery stays editable and idempotent retry opens task results", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".engine-state")).toContainText("Metal");
  await stage(page, "preview");
  await page.getByLabel("宽度", { exact: true }).fill("64");
  await page.getByLabel("高度", { exact: true }).fill("64");
  await page.getByLabel("总帧数", { exact: true }).fill("2");
  await stage(page, "delivery");
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  await target
    .getByLabel("封装 / 图像格式", { exact: true })
    .selectOption("exr");
  await page.getByRole("button", { name: "估算资源", exact: true }).click();
  await expect(page.locator(".estimate")).toBeVisible();
  await page
    .getByRole("button", { name: "在预览中修改规格", exact: true })
    .click();
  await page.getByLabel("总帧数", { exact: true }).fill("3");
  await stage(page, "delivery");
  await expect(page.locator(".estimate")).not.toBeVisible();
  const keys: string[] = [];
  await page.route(
    "**/api/operations/submit_configured_delivery",
    async (route) => {
      keys.push(route.request().postDataJSON().idempotency_key);
      if (keys.length === 1) await route.abort("failed");
      else await route.continue();
    },
  );
  await page.getByRole("button", { name: /^提交交付/ }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.locator("#workflow-title")).toHaveText("设置交付");
  await expect(
    page.getByRole("region", { name: "交付画面规格" }),
  ).toContainText("3 帧");
  await page.getByRole("button", { name: /^提交交付/ }).click();
  await expect(page.locator("#workflow-title")).toHaveText("任务与成果");
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  await expect(
    page.getByRole("button", { name: /^导出序列 delivery-exr-/ }),
  ).toBeVisible({ timeout: 20000 });
});

test("legacy daemon cannot silently restore the mixed color menu", async ({
  page,
}) => {
  await page.route("**/api/operations/get_delivery_options", async (route) => {
    const response = await route.fetch();
    const value = await response.json();
    delete value.options.color_space;
    delete value.options.gamma;
    delete value.color_encoding;
    await route.fulfill({ json: value });
  });
  await page.goto("/");
  await stage(page, "delivery");
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  await expect(target.getByRole("alert")).toContainText("本地服务需要更新");
  await expect(target.getByLabel("输出色彩", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^提交交付/ })).toBeDisabled();
  await page.unroute("**/api/operations/get_delivery_options");
  await target
    .getByRole("button", { name: "重试联动选项", exact: true })
    .click();
  await expect(target.getByLabel("输出色彩空间", { exact: true })).toHaveValue(
    "Rec.709",
  );
  await expect(target.getByRole("alert")).not.toBeVisible();
});

test("short desktop layouts keep preview and keyboard form controls reachable", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 700 });
  await page.goto("/");
  await stage(page, "preview");
  const media = await page.locator(".preview-stage").boundingBox();
  expect(media).not.toBeNull();
  expect(media!.y + media!.height).toBeLessThanOrEqual(700);
  await page.screenshot({
    path: "../reports/workflow-short-desktop.png",
    fullPage: true,
  });
  await stage(page, "delivery");
  await page.getByLabel("CRF", { exact: true }).focus();
  await page.keyboard.press("Tab");
  const interval = page.getByLabel("关键帧间隔（秒）", { exact: true });
  await expect(interval).toBeFocused();
  // A sticky review bar must never cover the focused field or its hit target.
  expect(
    await interval.evaluate((node) => {
      const r = node.getBoundingClientRect();
      return (
        document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) ===
        node
      );
    }),
  ).toBe(true);
  for (const name of ["scene", "preview", "delivery", "jobs"] as const) {
    await stage(page, name);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
});

test("new project creation guards a retained scene draft", async ({ page }) => {
  await page.goto("/");
  await stage(page, "preview");
  await page.locator(".code-disclosure > summary").click();
  const editor = page.getByLabel("WGSL 场景代码");
  const original = await editor.inputValue();
  await editor.fill(original + "\n// retain during project creation");
  await stage(page, "scene");
  await page.getByRole("button", { name: "新建项目", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "保留场景修改" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "新建项目" }),
  ).not.toBeVisible();
  await stage(page, "preview");
  await page.locator(".code-disclosure > summary").click();
  await expect(editor).toHaveValue(
    original + "\n// retain during project creation",
  );
  await stage(page, "scene");
  const project = page.getByLabel("当前项目", { exact: true });
  const current = await project.inputValue();
  const others = await project
    .locator("option")
    .evaluateAll(
      (options, selected) =>
        options
          .map((option) => (option as HTMLOptionElement).value)
          .filter((value) => value && value !== selected),
      current,
    );
  expect(others.length).toBeGreaterThan(0);
  await project.selectOption(others[0]);
  await expect(
    page.getByRole("dialog", { name: "保留场景修改" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "切换并舍弃", exact: true }).click();
  await expect(project).toHaveValue(others[0]);
});

test("clearing a project clears its task and results context", async ({
  page,
}) => {
  await page.goto("/");
  const project = page.getByLabel("当前项目", { exact: true });
  await expect(project).toHaveValue(/.+/);
  await project.selectOption("");
  await stage(page, "jobs");
  await expect(page.locator(".jobs")).toHaveCount(0);
  await expect(page.locator(".artifact-strip")).toHaveCount(0);
  await expect(
    page
      .getByRole("navigation", { name: "创作流程" })
      .getByRole("button", { name: "4. 任务与成果", exact: true }),
  ).toContainText("0 个任务");
});

test("original pixel inspection stays scoped to the preview step", async ({
  page,
}) => {
  let inspections = 0;
  await page.route("**/api/operations/inspect_frame", (route) => {
    inspections++;
    return route.fulfill({
      status: 400,
      json: { error: { message: "原始帧缓存不可用，请重新渲染预览。" } },
    });
  });
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "2. 编辑与预览", exact: true }),
  ).toContainText("已有画面预览");
  expect(inspections).toBe(0);
  await stage(page, "delivery");
  await expect(
    page.getByRole("region", { name: "交付画面规格" }),
  ).toBeVisible();
  expect(inspections).toBe(0);
  await expect(page.getByRole("alert")).not.toBeVisible();
  await stage(page, "preview");
  // Transient proxies never inspect a historical EXR; only the stored-frame view does.
  await expect(page.locator(".live-preview-image")).toBeVisible();
  expect(inspections).toBe(0);
  await page.getByRole("button", { name: "关键帧", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("原始帧缓存不可用");
  expect(inspections).toBe(1);
  await stage(page, "delivery");
  await expect(page.getByRole("alert")).not.toBeVisible();
});

test("blank WGSL scene starts without an example and opens the editor", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建项目", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "新建项目" });
  await page
    .getByLabel("项目名称", { exact: true })
    .fill("空白创作 " + Date.now());
  await dialog.getByRole("button", { name: "创建项目", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const created = page.waitForResponse(
    (r) => r.url().endsWith("/create_scene") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "新建空白场景", exact: true }).click();
  const response = await created;
  expect(response.request().postDataJSON().template_id).toBeNull();
  const value = await response.json();
  expect(value.scene.parameters).toEqual([]);
  await expect(
    page.getByRole("heading", { name: "空白场景", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("实时预览已更新", { exact: true })).toBeVisible({
    timeout: 20000,
  });
  await page.locator(".code-disclosure > summary").click();
  await expect(page.getByLabel("WGSL 场景代码")).toHaveValue(
    new RegExp("return vec4<f32>\\(0\\.0, 0\\.0, 0\\.0, 0\\.0\\)"),
  );
});
