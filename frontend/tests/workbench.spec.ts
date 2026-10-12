import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";

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
  await page.getByLabel("宽度", { exact: true }).fill("64");
  await page.getByLabel("高度", { exact: true }).fill("64");
  await page.getByLabel("总帧数", { exact: true }).fill("2");
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
  await page.getByRole("button", { name: /广色域渐变/ }).click();
  await expect(
    page.getByRole("dialog", { name: "保留场景修改" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(page.getByLabel("曝光（档）", { exact: true })).toHaveValue(
    "0.7",
  );
});

test("linked format choices preserve compatible values and repair conflicts", async ({
  page,
}) => {
  await page.goto("/");
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
  await field("输出色彩").selectOption("PQ");
  await expect(field("位深")).toHaveValue("10");
  await expect(field("编码档位")).toHaveValue("main10");
  await field("位深").selectOption("8");
  await expect(field("输出色彩")).toHaveValue("Rec.709");
  await field("封装 / 图像格式").selectOption("exr");
  await expect(field("输出色彩")).toHaveValue("ACEScg");
  await expect(field("数据范围")).toHaveValue("full");
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
  let results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  await page.getByRole("button", { name: "MCP 接口", exact: true }).click();
  results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../reports/workbench-mobile.png",
    fullPage: true,
  });
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
  const editor = page.getByLabel("WGSL 场景代码");
  if (await editor.isEnabled()) {
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
  await expect(page.getByLabel("WGSL 场景代码")).toBeEnabled();
  await page.getByLabel("宽度", { exact: true }).fill("64");
  await page.getByLabel("高度", { exact: true }).fill("64");
  await page.getByLabel("总帧数", { exact: true }).fill("2");
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  await target
    .getByLabel("封装 / 图像格式", { exact: true })
    .selectOption("exr");
  await expect(
    target.getByLabel("视频 / 图像编码", { exact: true }),
  ).toHaveValue("exr");
  await page.getByRole("button", { name: /^提交交付/ }).click();
  await page
    .getByRole("button", { name: /^导出序列 delivery-exr-/ })
    .click({ timeout: 20000 });
  const dialog = page.getByRole("dialog", { name: "导出图像序列" });
  const destination = dialog.getByLabel("导出目录", { exact: true });
  const original = await destination.inputValue();
  await destination.fill("");
  await dialog.getByRole("button", { name: "复制序列", exact: true }).click();
  await expect(dialog.getByText("请输入导出目录。")).toBeVisible();
  await destination.fill("/outside-not-allowed");
  await dialog.getByRole("button", { name: "复制序列", exact: true }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(destination).toHaveValue("/outside-not-allowed");
  await destination.fill(original);
  await dialog.getByRole("button", { name: "复制序列", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.locator(".export-result")).toContainText(original);
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
  const first = page.getByRole("group", { name: "交付目标 1", exact: true });
  await expect(
    first.getByLabel("封装 / 图像格式", { exact: true }),
  ).toBeEnabled();
  await page.getByLabel("帧率", { exact: true }).selectOption("120/1");
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
  await expect(page.getByLabel("帧率", { exact: true })).toHaveValue("60/1");
  await expect(
    page.getByLabel("帧率", { exact: true }).locator('option[value="120/1"]'),
  ).toHaveCount(0);
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
