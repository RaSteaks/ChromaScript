import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import type { Capability, Job, SceneReply } from "../src/types";

async function setup(page: Page) {
  const session = await (await page.request.get("/api/session")).json();
  async function call<T>(operation: string, args: unknown = {}): Promise<T> {
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
      name: "交付位置验证 " + Date.now(),
    },
  );
  const scene = await call<SceneReply>("create_scene", {
    project_id: project.id,
    template_id: "gradient",
  });
  const caps = await call<Capability>("get_capabilities");
  await page.goto("/");
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
  await page.getByLabel("宽度", { exact: true }).fill("64");
  await page.getByLabel("高度", { exact: true }).fill("36");
  await page.getByLabel("总帧数", { exact: true }).fill("2");
  await page.getByRole("button", { name: "3. 设置交付", exact: true }).click();
  return { call, caps, scene, project };
}

test("delivery destination validates paths, preserves drafts and exports a complete folder once", async ({
  page,
}) => {
  const { call, caps, scene, project } = await setup(page);
  const directory = page.getByLabel("导出位置", { exact: true });
  const submit = page.getByRole("button", { name: /^提交交付/ });
  await expect(directory).toHaveValue(caps.delivery_export!.default_directory);
  await directory.fill("");
  await submit.click();
  await expect(directory).toHaveAttribute("aria-invalid", "true");
  await expect(directory).toBeFocused();
  await expect(page.locator("#workflow-title")).toHaveText("设置交付");
  await directory.fill("relative/export");
  await submit.click();
  await expect(page.locator("#delivery-directory-help")).toContainText("绝对");
  expect(
    (await call<{ total: number }>("list_jobs", { project_id: project.id }))
      .total,
  ).toBe(0);

  // Choose a real destination outside the workspace with no startup grant.
  const parent = join(
    "/private/tmp",
    "ChromaScript 选择导出",
    String(Date.now()),
  );
  mkdirSync(parent, { recursive: true });
  expect(caps.delivery_export!.unrestricted).toBe(true);
  await directory.fill(caps.delivery_export!.default_directory);
  let selections = 0;
  await page.route("**/api/operations/choose_export_directory", (route) => {
    selections++;
    return route.fulfill({
      json:
        selections === 1
          ? { cancelled: true, directory: null }
          : { cancelled: false, directory: parent },
    });
  });
  await page.getByRole("button", { name: "选择文件夹", exact: true }).click();
  await expect(directory).toHaveValue(caps.delivery_export!.default_directory);
  await page.getByRole("button", { name: "选择文件夹", exact: true }).click();
  await expect(directory).toHaveValue(parent);
  await expect(page.getByRole("alert")).not.toBeVisible();
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
  await page.getByRole("button", { name: "3. 设置交付", exact: true }).click();
  await expect(directory).toHaveValue(parent);
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/delivery-export-desktop.png",
    fullPage: true,
  });

  // Lose the first successful response, then retry: the server must reuse the folder/job.
  const keys: string[] = [];
  await page.route(
    "**/api/operations/submit_configured_delivery",
    async (route) => {
      keys.push(route.request().postDataJSON().idempotency_key);
      expect(route.request().postDataJSON().export_directory).toBe(parent);
      if (keys.length === 1) {
        const response = await route.fetch();
        expect(response.ok()).toBe(true);
        await route.abort("failed");
      } else await route.continue();
    },
  );
  await submit.click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(directory).toHaveValue(parent);
  await submit.click();
  await expect(page.locator("#workflow-title")).toHaveText("任务与成果");
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  const result = page
    .getByRole("region", { name: "任务成果" })
    .locator(".export-result");
  await expect(result).toContainText("已导出至", { timeout: 20000 });
  const jobs = await call<{ items: Job[]; total: number }>("list_jobs", {
    project_id: project.id,
  });
  expect(jobs.total).toBe(1);
  const job = jobs.items[0];
  expect(job.export_completed).toBe(true);
  const folder = job.export_path!;
  expect(folder.startsWith(parent + "/")).toBe(true);
  expect(readdirSync(folder)).toEqual(
    expect.arrayContaining([
      "shader.wgsl",
      "scene.json",
      "scene-snapshot.json",
      "thumbnail.png",
      "verification.json",
      "delivery.json",
    ]),
  );
  expect(readFileSync(join(folder, "shader.wgsl"), "utf8")).toBe(
    scene.scene.code,
  );
  expect(JSON.parse(readFileSync(join(folder, "scene.json"), "utf8"))).toEqual(
    scene.scene,
  );
  const video = readdirSync(folder).find((name) => name.endsWith(".mp4"))!;
  expect(statSync(join(folder, video)).size).toBeGreaterThan(0);
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page
    .getByRole("button", { name: "复制交付文件夹路径", exact: true })
    .click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    folder,
  );
  let opens = 0;
  await page.route("**/api/operations/open_export_directory", (route) => {
    expect(route.request().postDataJSON().directory).toBe(folder);
    opens++;
    return route.fulfill(
      opens === 1
        ? {
            status: 400,
            json: {
              error: { code: "folder_open_failed", message: "测试打开失败" },
            },
          }
        : { json: { directory: folder } },
    );
  });
  await result
    .getByRole("button", { name: "打开导出文件夹", exact: true })
    .click();
  await expect(result.getByRole("alert")).toContainText("测试打开失败");
  await result
    .getByRole("button", { name: "打开导出文件夹", exact: true })
    .click();
  await expect(result.getByRole("alert")).not.toBeVisible();
  expect(opens).toBe(2);
});

test("delivery destination accessibility, narrow layout and changed-path retry identity", async ({
  page,
}) => {
  const { caps } = await setup(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const directory = page.getByLabel("导出位置", { exact: true });
  await page.route("**/api/operations/choose_export_directory", (route) =>
    route.fulfill({ json: { cancelled: true, directory: null } }),
  );
  await page.getByRole("button", { name: "选择文件夹", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(directory).toHaveValue(caps.delivery_export!.default_directory);
  await page.getByRole("button", { name: "使用默认位置", exact: true }).click();
  await expect(directory).toHaveValue(caps.delivery_export!.default_directory);
  await directory.fill(
    caps.delivery_export!.default_directory +
      "/这是一个较长的中文导出目录-用于检查移动设备布局",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const scan = await new AxeBuilder({ page })
    .include(".step-delivery")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(scan.violations).toEqual([]);
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/delivery-export-mobile.png",
    fullPage: true,
  });
  const keys: string[] = [];
  await page.route("**/api/operations/submit_configured_delivery", (route) => {
    keys.push(route.request().postDataJSON().idempotency_key);
    return route.abort("failed");
  });
  const submit = page.getByRole("button", { name: /^提交交付/ });
  await submit.click();
  await expect(page.getByRole("alert")).toBeVisible();
  await directory.fill(caps.delivery_export!.default_directory + "/different");
  await submit.click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[0]).not.toBe(keys[1]);
});
