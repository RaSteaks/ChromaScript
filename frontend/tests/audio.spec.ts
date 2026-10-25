import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// The former modal now has retained material/timeline tabs in the Sound stage.
test("project audio import, synthesis, drafts, conflicts and accessibility", async ({
  page,
}, info) => {
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
  await page
    .getByRole("button", { name: "项目音频", exact: true })
    .press("Enter");
  await page.getByRole("button", { name: "音频素材", exact: true }).click();
  const editor = page.locator(".project-audio");
  await editor.getByLabel("音频名称", { exact: true }).fill("测试节奏");
  const waveform = editor.getByLabel("波形", { exact: true });
  await waveform.focus();
  await waveform.press("ArrowDown");
  await waveform.press("Enter");
  await waveform.selectOption("sine");
  await editor.getByLabel("时长（秒）", { exact: true }).fill("0.2");
  await editor.getByRole("button", { name: "生成音频", exact: true }).click();
  await expect(
    editor.getByRole("button", { name: "添加 测试节奏 到音轨" }),
  ).toBeVisible();
  const audio = editor.getByLabel("试听 测试节奏", { exact: true });
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
  await editor
    .getByLabel("导入本地音频", { exact: true })
    .setInputFiles({
      name: "外部音效.wav",
      mimeType: "audio/wav",
      buffer: await wav.body(),
    });
  await editor.getByRole("button", { name: "导入音频", exact: true }).click();
  await expect(
    editor.getByRole("button", { name: "添加 外部音效.wav 到音轨" }),
  ).toBeVisible();
  await editor.getByRole("button", { name: "添加 测试节奏 到音轨" }).click();
  await page.getByRole("button", { name: /^时间线/ }).click();
  await editor.getByLabel("增益（dB）", { exact: true }).fill("bad");
  await editor.getByRole("button", { name: "保存音轨", exact: true }).click();
  await expect(editor.getByRole("alert")).toContainText("请输入");
  await expect(editor.getByLabel("增益（dB）", { exact: true })).toBeFocused();
  await editor.getByLabel("增益（dB）", { exact: true }).fill("-12");
  await editor.getByRole("button", { name: "保存音轨", exact: true }).click();
  await expect(
    editor.getByText("音轨已保存，将用于此项目的新交付任务。"),
  ).toBeVisible();
  const saved = await call("get_project_audio", { project_id: project.id });
  expect(saved.timeline.clips[0].gain_db).toBe(-12);
  await editor.getByLabel("时间线起点（秒）", { exact: true }).fill("0.1");
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
  await page.getByRole("button", { name: "3. 声音", exact: true }).click();
  await expect(
    editor.getByLabel("时间线起点（秒）", { exact: true }),
  ).toHaveValue("0.1");
  await call("set_project_audio", {
    project_id: project.id,
    expected_revision_id: saved.revision_id,
    timeline: { clips: [] },
  });
  await editor.getByRole("button", { name: "保存音轨", exact: true }).click();
  await expect(editor.getByRole("alert")).toContainText("草稿已保留");
  await editor.getByRole("button", { name: "重新读取最新音轨" }).click();
  await page
    .getByRole("dialog", { name: "放弃音轨草稿？" })
    .getByRole("button", { name: "继续编辑" })
    .click();
  await expect(
    editor.getByLabel("时间线起点（秒）", { exact: true }),
  ).toHaveValue("0.1");
  await editor.getByRole("button", { name: "重新读取最新音轨" }).click();
  await page
    .getByRole("dialog", { name: "放弃音轨草稿？" })
    .getByRole("button", { name: "放弃草稿" })
    .click();
  await expect(
    editor.getByText("当前没有音轨，交付将保持静音。"),
  ).toBeVisible();
  await page.getByRole("button", { name: "音频素材", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    (await new AxeBuilder({ page }).include(".sound-workspace").analyze())
      .violations,
  ).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("audio-mobile.png"),
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
  await page.getByRole("button", { name: "音频素材", exact: true }).click();
  const editor = page.locator(".project-audio");
  await expect(editor.getByRole("alert")).toContainText("音频服务暂时不可用");
  await page.unroute("**/api/operations/get_project_audio");
  await editor.getByRole("button", { name: "重新读取音频" }).click();
  await expect(
    editor.getByRole("button", { name: "生成音频", exact: true }),
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
  await editor.getByLabel("音频名称", { exact: true }).fill("保留的名称");
  await editor.getByRole("button", { name: "生成音频", exact: true }).click();
  await expect(
    editor.getByRole("button", { name: "生成音频", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "方向试听", exact: true }).click();
  release();
  await page.getByRole("button", { name: "音频素材", exact: true }).click();
  await expect(editor.getByRole("alert")).toContainText("生成失败");
  await expect(editor.getByLabel("音频名称", { exact: true })).toHaveValue(
    "保留的名称",
  );
  await expect(
    editor.getByRole("button", { name: "生成音频", exact: true }),
  ).toBeEnabled();
});
