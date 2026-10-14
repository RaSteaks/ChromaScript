import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import type { MotionReply, MotionSpec } from "../src/types";

const execute = promisify(execFile);
const python = fileURLToPath(
  new URL("../../.venv/bin/python", import.meta.url),
);
const source = (label: string) =>
  `import React from "react";
export default function Animation() {
  return <div style={{width: "100%", height: "100%", background: "#243746", color: "white"}}>${label}</div>;
}`;
const editor = (page: Page) =>
  page.getByRole("textbox", { name: "动画 TypeScript 代码", exact: true });
const connection = (page: Page) =>
  page.getByRole("button", { name: "刷新动画列表与连接", exact: true });
const conflict = (page: Page) =>
  page.getByRole("status").filter({ hasText: "检测到外部更新" });

async function setup(page: Page) {
  const session = await (await page.request.get("/api/session")).json();
  const health = await (await page.request.get("/health")).json();
  async function call<T>(name: string, args: unknown): Promise<T> {
    const response = await page.request.post("/api/operations/" + name, {
      headers: { Authorization: "Bearer " + session.token },
      data: args,
    });
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  }
  const { project } = await call<{ project: { id: string } }>(
    "create_project",
    {
      name: "动画同步验证 " + Date.now(),
    },
  );
  const spec: MotionSpec = {
    engine: "remotion-sdr-v1",
    schema_version: 1,
    name: "同步初稿",
    width: 320,
    height: 180,
    frames: 24,
    fps: { numerator: 30000, denominator: 1001 },
    code: source("同步初稿"),
  };
  const saved = await call<MotionReply>("create_motion_scene", {
    project_id: project.id,
    spec,
  });

  // Exercise the real MCP adapter with the locked Python SDK. Credentials stay
  // in the child environment, never command arguments, screenshots or reports.
  async function mcpUpdate(base: MotionReply, changes: Partial<MotionSpec>) {
    const { stdout } = await execute(
      python,
      [
        "-c",
        `import asyncio, json, os
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + os.environ["CHROMASCRIPT_SYNC_TOKEN"]}) as http:
        async with streamable_http_client(os.environ["CHROMASCRIPT_SYNC_URL"], http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as client:
                await client.initialize()
                reply = await client.call_tool("chromascript.update_motion_scene", {"request": json.loads(os.environ["CHROMASCRIPT_SYNC_ARGS"])})
                assert not reply.is_error, reply.structured_content
                print(json.dumps(reply.structured_content))

asyncio.run(main())`,
      ],
      {
        timeout: 20000,
        env: {
          ...process.env,
          CHROMASCRIPT_SYNC_URL: new URL("/mcp/", page.url()).href,
          CHROMASCRIPT_SYNC_TOKEN: session.token,
          CHROMASCRIPT_SYNC_ARGS: JSON.stringify({
            scene_id: base.record.id,
            expected_revision_id: base.revision_id,
            spec: { ...base.scene, ...changes },
          }),
        },
      },
    );
    return JSON.parse(stdout) as MotionReply;
  }

  async function revisionCount() {
    // Read SQLite in read-only mode: polling must never append hidden history.
    const { stdout } = await execute(
      python,
      [
        "-c",
        `import os, sqlite3
from pathlib import Path
with sqlite3.connect((Path(os.environ["CHROMASCRIPT_SYNC_WORKSPACE"]) / "state.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
    print(db.execute("SELECT COUNT(*) FROM records WHERE kind='motion_revision' AND json_extract(data, '$.scene_id')=?", (os.environ["CHROMASCRIPT_SYNC_SCENE"],)).fetchone()[0])`,
      ],
      {
        env: {
          ...process.env,
          CHROMASCRIPT_SYNC_WORKSPACE: health.workspace,
          CHROMASCRIPT_SYNC_SCENE: saved.record.id,
        },
      },
    );
    return Number(stdout.trim());
  }

  await page.goto("/");
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await page.getByLabel("动画项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("已保存的动画", { exact: true })
    .selectOption(saved.record.id);
  await expect(
    page.frameLocator("iframe[title='动画画面']").getByText("同步初稿"),
  ).toBeVisible({ timeout: 20000 });
  return { call, saved, project, mcpUpdate, revisionCount };
}

test("MCP updates the existing editor, specification and preview without reloading or writing history", async ({
  page,
}) => {
  const { saved, project, call, mcpUpdate, revisionCount } = await setup(page);
  const mounted = await editor(page).elementHandle();
  let navigations = 0;
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) navigations += 1;
  });
  await editor(page).focus();
  const latest = await mcpUpdate(saved, {
    name: "MCP 更新名称",
    code: source("MCP 自动同步画面"),
    width: 352,
    height: 198,
    frames: 18,
    fps: { numerator: 24000, denominator: 1001 },
  });
  await expect(editor(page)).toContainText("MCP 自动同步画面");
  await expect(editor(page)).toBeFocused();
  expect(await mounted!.evaluate((node) => node.isConnected)).toBe(true);
  await expect(page.getByLabel("动画名称", { exact: true })).toHaveValue(
    latest.scene.name,
  );
  for (const [label, value] of [
    ["宽度", "352"],
    ["高度", "198"],
    ["总帧数", "18"],
    ["帧率", "24000/1001"],
  ])
    await expect(page.getByLabel(label, { exact: true })).toHaveValue(value);
  await expect(page.getByLabel("已保存的动画", { exact: true })).toContainText(
    latest.scene.name,
  );
  await expect(page.locator(".motion-workbench .save-state")).toContainText(
    latest.revision_id.slice(0, 8),
  );
  await expect(
    page.getByRole("button", { name: "保存动画", exact: true }),
  ).toBeDisabled();
  await expect(
    page.frameLocator("iframe[title='动画画面']").getByText("MCP 自动同步画面"),
  ).toBeVisible({ timeout: 20000 });
  await expect(
    page.getByRole("button", { name: "播放动画", exact: true }),
  ).toBeEnabled();
  // Multiple unchanged reads retain the same editor and immutable saved head.
  await page.waitForTimeout(2200);
  expect(navigations).toBe(0);
  expect(await revisionCount()).toBe(2);
  const jobs = await call<{ total: number }>("list_jobs", {
    project_id: project.id,
    kind: "motion",
  });
  expect(jobs.total).toBe(0);
});

test("external updates retain all draft fields, the save baseline and explicit save errors", async ({
  page,
}) => {
  const { saved, mcpUpdate } = await setup(page);
  await editor(page).fill(source("本地未保存代码"));
  await page.getByLabel("动画名称", { exact: true }).fill("本地未保存名称");
  await page.getByLabel("宽度", { exact: true }).fill("333");
  await page.getByLabel("总帧数", { exact: true }).fill("20");
  await page.getByLabel("帧率", { exact: true }).selectOption("24/1");
  await page.route("**/api/operations/update_motion_scene", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { code: "unavailable", message: "保存检查失败" } },
    }),
  );
  await page.getByRole("button", { name: "保存动画", exact: true }).click();
  const saveError = page.getByRole("alert").filter({ hasText: "保存检查失败" });
  await expect(saveError).toBeVisible();
  const remote = await mcpUpdate(saved, {
    name: "外部名称",
    code: source("外部代码"),
    width: 352,
  });
  await expect(conflict(page)).toBeVisible();
  await expect(saveError).toBeVisible();
  await expect(editor(page)).toContainText("本地未保存代码");
  for (const [label, value] of [
    ["动画名称", "本地未保存名称"],
    ["宽度", "333"],
    ["总帧数", "20"],
    ["帧率", "24/1"],
  ])
    await expect(page.getByLabel(label, { exact: true })).toHaveValue(value);
  await page.unroute("**/api/operations/update_motion_scene");
  let expectedRevision = "";
  await page.route("**/api/operations/update_motion_scene", (route) => {
    expectedRevision = route.request().postDataJSON().expected_revision_id;
    return route.continue();
  });
  await page.getByRole("button", { name: "保存动画", exact: true }).click();
  await expect.poll(() => expectedRevision).toBe(saved.revision_id);
  const latest = page.getByRole("button", {
    name: "载入最新版本",
    exact: true,
  });
  await latest.click();
  await expect(
    page.getByRole("dialog", { name: "保留当前动画草稿" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(editor(page)).toContainText("本地未保存代码");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(
    (await new AxeBuilder({ page }).exclude("iframe").analyze()).violations,
  ).toEqual([]);
  await latest.click();
  await page
    .getByRole("button", { name: "丢弃修改并继续", exact: true })
    .click();
  await expect(editor(page)).toContainText("外部代码");
  await expect(page.getByLabel("动画名称", { exact: true })).toHaveValue(
    remote.scene.name,
  );
  await expect(conflict(page)).not.toBeVisible();
});

test("a read started before typing cannot replace a newer local draft", async ({
  page,
}) => {
  const { saved, mcpUpdate } = await setup(page);
  let start: () => void = () => {};
  let release: () => void = () => {};
  const started = new Promise<void>((resolve) => {
    start = resolve;
  });
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let remote = saved;
  let held = false;
  await page.route("**/api/operations/get_motion_scene", async (route) => {
    if (held) return route.continue();
    held = true;
    start();
    await gate;
    return route.fulfill({ json: remote });
  });
  try {
    await started;
    remote = await mcpUpdate(saved, { code: source("迟到的外部代码") });
    await editor(page).fill(source("读取期间输入的草稿"));
  } finally {
    release();
  }
  await expect(conflict(page)).toBeVisible();
  await expect(editor(page)).toContainText("读取期间输入的草稿");
  await expect(editor(page)).not.toContainText("迟到的外部代码");
});

test("saving suspends reads and a late acknowledgement preserves edits and subsequent MCP changes", async ({
  page,
}) => {
  const { mcpUpdate } = await setup(page);
  let release: () => void = () => {};
  let committed: (reply: MotionReply) => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const commit = new Promise<MotionReply>((resolve) => {
    committed = resolve;
  });
  await page.route("**/api/operations/update_motion_scene", async (route) => {
    const response = await route.fetch();
    const reply = await response.json();
    committed(reply);
    await gate;
    return route.fulfill({ response });
  });
  let reads = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/get_motion_scene")) reads += 1;
  });
  await editor(page).fill(source("本次保存代码"));
  await page.getByRole("button", { name: "保存动画", exact: true }).click();
  try {
    const acknowledged = await commit;
    const pausedAt = reads;
    await page.waitForTimeout(1200);
    expect(reads).toBe(pausedAt);
    await mcpUpdate(acknowledged, { code: source("保存之后的 MCP 代码") });
    await editor(page).fill(source("保存等待期间的新草稿"));
  } finally {
    release();
  }
  await expect(conflict(page)).toBeVisible();
  await expect(editor(page)).toContainText("保存等待期间的新草稿");
  await expect(
    page.getByRole("button", { name: "保存动画", exact: true }),
  ).toBeEnabled();
});

test("a late read cannot reopen an animation after selection changes", async ({
  page,
}) => {
  const { saved, call, project } = await setup(page);
  const other = await call<MotionReply>("create_motion_scene", {
    project_id: project.id,
    spec: { ...saved.scene, name: "另一动画", code: source("另一动画画面") },
  });
  await connection(page).click();
  await expect(page.getByLabel("已保存的动画", { exact: true })).toContainText(
    other.scene.name,
  );
  let release: () => void = () => {};
  let start: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const started = new Promise<void>((resolve) => {
    start = resolve;
  });
  await page.route("**/api/operations/get_motion_scene", async (route) => {
    if (route.request().postDataJSON().scene_id !== saved.record.id)
      return route.continue();
    start();
    await gate;
    return route.fulfill({
      json: {
        ...saved,
        revision_id: "obsolete-read",
        scene: { ...saved.scene, code: source("过期画面") },
      },
    });
  });
  try {
    await started;
    await page
      .getByLabel("已保存的动画", { exact: true })
      .selectOption(other.record.id);
    await expect(editor(page)).toContainText("另一动画画面");
  } finally {
    release();
  }
  await page.waitForTimeout(1200);
  await expect(page.getByLabel("已保存的动画", { exact: true })).toHaveValue(
    other.record.id,
  );
  await expect(editor(page)).toContainText("另一动画画面");
  await expect(editor(page)).not.toContainText("过期画面");
});

test("IME, inactive views and hidden tabs pause reads and immediately recheck on return", async ({
  page,
}) => {
  const { saved, mcpUpdate } = await setup(page);
  let reads = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/get_motion_scene")) reads += 1;
  });
  const name = page.getByLabel("动画名称", { exact: true });
  await name.dispatchEvent("compositionstart");
  let latest = await mcpUpdate(saved, { code: source("组合输入后的外部代码") });
  const composingAt = reads;
  await page.waitForTimeout(1200);
  expect(reads).toBe(composingAt);
  await expect(editor(page)).toContainText("同步初稿");
  await name.dispatchEvent("compositionend");
  await expect(editor(page)).toContainText("组合输入后的外部代码");

  await page.getByRole("button", { name: "工作区", exact: true }).click();
  latest = await mcpUpdate(latest, { code: source("返回动画页的外部代码") });
  const inactiveAt = reads;
  await page.waitForTimeout(1200);
  expect(reads).toBe(inactiveAt);
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await expect(editor(page)).toContainText("返回动画页的外部代码");

  // Model document lifecycle explicitly; headless multi-page focus does not
  // reliably generate visibilitychange on every supported Chromium platform.
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await mcpUpdate(latest, { code: source("重新可见的外部代码") });
  const hiddenAt = reads;
  await page.waitForTimeout(1200);
  expect(reads).toBe(hiddenAt);
  await expect(editor(page)).toContainText("返回动画页的外部代码");
  await page.evaluate(() => {
    delete (document as unknown as Record<string, unknown>).hidden;
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(editor(page)).toContainText("重新可见的外部代码");
});

test("three failed reads stop synchronization and the existing connection control recovers", async ({
  page,
}) => {
  const { saved, mcpUpdate } = await setup(page);
  let attempts = 0;
  const times: number[] = [];
  await page.route("**/api/operations/get_motion_scene", (route) => {
    attempts += 1;
    times.push(Date.now());
    return route.fulfill({
      status: 503,
      json: { error: { code: "unavailable", message: "同步读取测试失败" } },
    });
  });
  await expect(
    page.getByRole("alert").filter({ hasText: "自动同步已暂停" }),
  ).toBeVisible({ timeout: 12000 });
  expect(attempts).toBe(3);
  expect(times[1] - times[0]).toBeGreaterThanOrEqual(1800);
  expect(times[2] - times[1]).toBeGreaterThanOrEqual(3800);
  await mcpUpdate(saved, { code: source("连接恢复的外部代码") });
  await page.waitForTimeout(1200);
  expect(attempts).toBe(3);
  await expect(editor(page)).toContainText("同步初稿");
  await page.unroute("**/api/operations/get_motion_scene");
  await connection(page).click();
  await expect(editor(page)).toContainText("连接恢复的外部代码");
  await expect(
    page.getByRole("alert").filter({ hasText: "同步读取测试失败" }),
  ).not.toBeVisible();
});

test("a hanging read times out without replacing the code or working picture", async ({
  page,
}) => {
  const { saved } = await setup(page);
  let start: () => void = () => {};
  let release: () => void = () => {};
  const started = new Promise<void>((resolve) => {
    start = resolve;
  });
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const picture = await page
    .locator("iframe[title='动画画面']")
    .getAttribute("srcdoc");
  await page.route("**/api/operations/get_motion_scene", async (route) => {
    start();
    await gate;
    return route.fulfill({ json: saved });
  });
  try {
    await started;
    await expect(
      page.getByRole("alert").filter({ hasText: "暂时无法同步动画" }),
    ).toBeVisible({ timeout: 12000 });
    await expect(editor(page)).toContainText("同步初稿");
    await expect(page.locator("iframe[title='动画画面']")).toHaveAttribute(
      "srcdoc",
      picture!,
    );
  } finally {
    release();
  }
  await expect(
    page.getByRole("alert").filter({ hasText: "暂时无法同步动画" }),
  ).not.toBeVisible();
});
