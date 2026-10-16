import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// Exercise real audio operations and native media URLs in an isolated project.
test("project audio import, synthesis, drafts, conflicts and accessibility", async ({
  page,
}) => {
  const session = await (await page.request.get("/api/session")).json();
  async function call(name: string, data: unknown) {
    const reply = await page.request.post(`/api/operations/${name}`, {
      headers: { Authorization: `Bearer ${session.token}` },
      data,
    });
    expect(reply.ok(), await reply.text()).toBe(true);
    return reply.json();
  }
  const { project } = await call("create_project", {
    name: `音频验证 ${Date.now()}`,
  });
  await page.goto("/");
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  // WebKit does not focus buttons on a pointer click; begin from keyboard focus
  // so the existing restoration assertion tests a real prior focus target.
  await page.getByRole("button", { name: "项目音频", exact: true }).focus();
  await page.getByRole("button", { name: "项目音频", exact: true }).press("Enter");
  const dialog = page.getByRole("dialog", { name: "项目音频", exact: true });
  await expect(
    dialog.getByText("当前没有音轨，交付将保持静音。"),
  ).toBeVisible();
  await dialog.getByLabel("音频名称", { exact: true }).fill("测试节奏");
  const waveform = dialog.getByLabel("波形", { exact: true });
  await waveform.focus();
  await waveform.press("ArrowDown");
  await waveform.press("Enter");
  await waveform.selectOption("sine");
  await dialog.getByLabel("时长（秒）", { exact: true }).fill("0.2");
  await dialog.getByRole("button", { name: "生成音频", exact: true }).click();
  await expect(
    dialog.getByRole("button", { name: "添加 测试节奏 到音轨" }),
  ).toBeVisible();
  const audio = dialog.getByLabel("试听 测试节奏", { exact: true });
  await expect(audio).toBeVisible();
  // Native controls consume the actual authenticated 32-bit PCM WAV.
  expect(
    await audio.evaluate((node) => (node as HTMLAudioElement).paused),
  ).toBe(true);
  await audio.evaluate((node) => (node as HTMLAudioElement).load());
  await expect
    .poll(() => audio.evaluate((node) => (node as HTMLAudioElement).readyState))
    .toBeGreaterThan(0);
  expect(
    await audio.evaluate((node) => (node as HTMLAudioElement).duration),
  ).toBeCloseTo(0.2, 3);
  const reply = await call("get_project_audio", { project_id: project.id });
  const wav = await page.request.get(`/api/artifacts/${reply.assets[0].id}`);
  await dialog
    .getByLabel("导入本地音频", { exact: true })
    .setInputFiles({
      name: "外部音效.wav",
      mimeType: "audio/wav",
      buffer: await wav.body(),
    });
  await dialog.getByRole("button", { name: "导入音频", exact: true }).click();
  await expect(
    dialog.getByRole("button", { name: "添加 外部音效.wav 到音轨" }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "添加 测试节奏 到音轨" }).click();
  await dialog.getByLabel("增益（dB）", { exact: true }).fill("bad");
  await dialog.getByRole("button", { name: "保存音轨", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("请输入");
  await expect(dialog.getByLabel("增益（dB）", { exact: true })).toBeFocused();
  await expect(dialog.getByLabel("增益（dB）", { exact: true })).toHaveValue(
    "bad",
  );
  await dialog.getByLabel("增益（dB）", { exact: true }).fill("-12");
  await dialog.getByRole("button", { name: "保存音轨", exact: true }).click();
  await expect(
    dialog.getByText("音轨已保存，将用于此项目的新交付任务。"),
  ).toBeVisible();
  const saved = await call("get_project_audio", { project_id: project.id });
  expect(saved.timeline.clips[0].gain_db).toBe(-12);
  await dialog.getByLabel("时间线起点（秒）", { exact: true }).fill("0.1");
  await call("set_project_audio", {
    project_id: project.id,
    expected_revision_id: saved.revision_id,
    timeline: { clips: [] },
  });
  await dialog.getByRole("button", { name: "保存音轨", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("草稿已保留");
  await expect(
    dialog.getByLabel("时间线起点（秒）", { exact: true }),
  ).toHaveValue("0.1");
  await dialog.getByRole("button", { name: "重新读取最新音轨" }).click();
  await page
    .getByRole("dialog", { name: "放弃音轨草稿？" })
    .getByRole("button", { name: "继续编辑" })
    .click();
  await expect(
    dialog.getByLabel("时间线起点（秒）", { exact: true }),
  ).toHaveValue("0.1");
  await dialog.getByRole("button", { name: "重新读取最新音轨" }).click();
  await page
    .getByRole("dialog", { name: "放弃音轨草稿？" })
    .getByRole("button", { name: "放弃草稿" })
    .click();
  await expect(
    dialog.getByText("当前没有音轨，交付将保持静音。"),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    dialog.getByRole("button", { name: "生成音频", exact: true }),
  ).toBeVisible();
  const result = await new AxeBuilder({ page })
    .include('[role="dialog"], dialog')
    .analyze();
  expect(result.violations).toEqual([]);
  expect(
    await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
  ).toBe(true);
  await page.screenshot({
    path: "/private/tmp/chromascript-audio-mobile.png",
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "项目音频", exact: true }),
  ).toBeFocused();
  // The second workspace reuses the same project timeline and component.
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await page.getByLabel("动画项目", { exact: true }).selectOption(project.id);
  await page.getByRole("button", { name: "项目音频", exact: true }).click();
  await expect(
    dialog.getByRole("button", { name: "添加 测试节奏 到音轨" }),
  ).toBeVisible();
  await page.screenshot({
    path: "/private/tmp/chromascript-audio-desktop.png",
    fullPage: true,
  });
});

test("audio loading failures and pending writes preserve the editor", async ({
  page,
}) => {
  const session = await (await page.request.get("/api/session")).json();
  const response = await page.request.post("/api/operations/create_project", {
    headers: { Authorization: `Bearer ${session.token}` },
    data: { name: `音频恢复 ${Date.now()}` },
  });
  const { project } = await response.json();
  await page.goto("/");
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page.route("**/api/operations/get_project_audio", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "音频服务暂时不可用" } },
    }),
  );
  await page.getByRole("button", { name: "项目音频", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "项目音频", exact: true });
  await expect(dialog.getByRole("alert")).toContainText("音频服务暂时不可用");
  await page.unroute("**/api/operations/get_project_audio");
  await dialog.getByRole("button", { name: "重新读取音频" }).click();
  await expect(
    dialog.getByRole("button", { name: "生成音频", exact: true }),
  ).toBeEnabled();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/operations/create_audio", async (route) => {
    await gate;
    await route.fulfill({
      status: 503,
      json: { error: { message: "生成失败，请重试" } },
    });
  });
  await dialog.getByLabel("音频名称", { exact: true }).fill("保留的名称");
  await dialog.getByRole("button", { name: "生成音频", exact: true }).click();
  await expect(
    dialog.getByText("正在生成音频…", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "生成音频", exact: true }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  release();
  await expect(dialog.getByRole("alert")).toContainText("生成失败");
  await expect(dialog.getByLabel("音频名称", { exact: true })).toHaveValue(
    "保留的名称",
  );
  await expect(
    dialog.getByRole("button", { name: "生成音频", exact: true }),
  ).toBeEnabled();
});
