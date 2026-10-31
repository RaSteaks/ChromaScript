import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function fixture(page: Page) {
  const session = await (await page.request.get("/api/session")).json();
  async function call(operation: string, data: unknown) {
    const reply = await page.request.post(`/api/operations/${operation}`, {
      headers: { Authorization: `Bearer ${session.token}` },
      data,
    });
    expect(reply.ok(), await reply.text()).toBe(true);
    return reply.json();
  }
  const { project } = await call("create_project", {
    name: `配音验证 ${Date.now()}`,
  });
  // The asset is local and decodable. Only the costly upstream boundary is fake.
  const generated = await call("create_audio", {
    project_id: project.id,
    name: "本地配音测试源",
    spec: {
      duration_seconds: 0.1,
      events: [
        {
          waveform: "sine",
          duration_seconds: 0.1,
          frequency_hz: 440,
          gain_db: -18,
        },
      ],
    },
  });
  const asset = {
    ...generated.asset,
    name: "配音测试",
    metadata: { ...generated.asset.metadata, source: "openai_tts" },
  };
  const calls: { operation: string; data: Record<string, unknown> }[] = [];
  let configured = true;
  let configFailure = false;
  await page.route("**/api/operations/get_speech_config", async (route) => {
    if (configFailure) {
      await route.fulfill({
        status: 503,
        json: { error: { message: "配音配置暂时不可用" } },
      });
      return;
    }
    const reply = await route.fetch();
    await route.fulfill({
      json: { ...(await reply.json()), api_key_configured: configured },
    });
  });
  // A catch-all prevents a test failure from ever falling through to cloud generation.
  await page.route("**/api/operations/generate_speech", async (route) => {
    calls.push({
      operation: "generate_speech",
      data: route.request().postDataJSON(),
    });
    await route.fulfill({ json: { asset, request_id: "test-request" } });
  });
  await page.goto("/");
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page.getByRole("button", { name: "项目音频", exact: true }).click();
  await page.getByRole("button", { name: /^音频素材/ }).click();
  const form = page.getByRole("region", { name: "云端配音", exact: true });
  await expect(form.getByLabel("配音供应商", { exact: true })).toHaveValue("");
  return {
    project,
    asset,
    calls,
    call,
    form,
    setConfigured(value: boolean) {
      configured = value;
    },
    setConfigFailure(value: boolean) {
      configFailure = value;
    },
  };
}

test("cloud speech providers, paid confirmation, assets and accessibility", async ({
  page,
}, info) => {
  const f = await fixture(page);
  const supplier = f.form.getByLabel("配音供应商", { exact: true });
  await f.form.getByRole("button", { name: "生成配音", exact: true }).click();
  await expect(supplier).toBeFocused();
  await expect(f.form.getByRole("alert")).toContainText("请选择配音供应商");
  await f.form.getByLabel("配音名称", { exact: true }).fill("配音测试");
  await f.form
    .getByLabel("配音台词", { exact: true })
    .fill("城市渐渐安静下来。");
  await f.form
    .getByLabel("演绎要求", { exact: true })
    .fill("温暖、克制的普通话旁白。");
  await supplier.selectOption("openai");
  await expect(
    f.form.getByText("密钥已配置，尚未验证", { exact: true }),
  ).toBeVisible();
  await expect(f.form.getByLabel("配音模型", { exact: true })).toHaveValue(
    "gpt-realtime-2.1-mini",
  );
  await f.form.getByLabel("配音音色", { exact: true }).selectOption("cedar");
  await supplier.selectOption("openrouter");
  await expect(f.form.getByLabel("配音音色", { exact: true })).toHaveValue(
    "Kore",
  );
  await f.form.getByLabel("配音音色", { exact: true }).fill("Puck");
  await supplier.selectOption("openai");
  await expect(f.form.getByLabel("配音音色", { exact: true })).toHaveValue(
    "cedar",
  );
  await expect(f.form.getByLabel("配音台词", { exact: true })).toHaveValue(
    "城市渐渐安静下来。",
  );
  await expect(
    f.form.getByRole("button", { name: "生成配音", exact: true }),
  ).toBeEnabled();
  const generate = f.form.getByRole("button", {
    name: "生成配音",
    exact: true,
  });
  await generate.press("Enter");
  const confirmation = page.getByRole("dialog", { name: "确认生成云端配音" });
  await expect(confirmation).toContainText("OpenAI");
  await expect(confirmation).toContainText("cedar");
  await expect(confirmation).toContainText("可能产生费用");
  await confirmation.getByRole("button", { name: "取消", exact: true }).click();
  await expect(generate).toBeFocused();
  expect(f.calls).toEqual([]);

  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/operations/generate_speech", async (route) => {
    f.calls.push({
      operation: "generate_speech",
      data: route.request().postDataJSON(),
    });
    await gate;
    await route.fulfill({
      json: { asset: f.asset, request_id: "test-request" },
    });
  });
  await generate.click();
  await confirmation
    .getByRole("button", { name: "确认生成并可能付费" })
    .click();
  await expect(
    f.form.getByRole("button", { name: "生成新一版配音" }),
  ).toBeDisabled();
  await expect(
    f.form.getByText("正在生成配音，请等待…", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
  await page.getByRole("button", { name: "3. 声音", exact: true }).click();
  await expect(f.form.getByLabel("配音台词", { exact: true })).toHaveValue(
    "城市渐渐安静下来。",
  );
  expect(f.calls).toHaveLength(1);
  expect(f.calls[0].data).toMatchObject({
    project_id: f.project.id,
    name: "配音测试",
    request: { provider: "openai", voice: "cedar", response_format: "pcm" },
  });
  release();
  await expect(
    f.form.getByText("AI 生成配音已加入素材，可试听并添加到音轨。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "添加 配音测试 到音轨" }),
  ).toHaveCount(1);
  await expect(
    page.getByText(/配音测试 · 0.10 秒 · AI 生成配音/),
  ).toBeVisible();
  const audio = page.getByLabel("试听 配音测试", { exact: true });
  expect(
    await audio.evaluate((element) => (element as HTMLAudioElement).paused),
  ).toBe(true);
  await audio.evaluate((element) => (element as HTMLAudioElement).load());
  await expect
    .poll(() =>
      audio.evaluate((element) => (element as HTMLAudioElement).readyState),
    )
    .toBeGreaterThan(0);
  expect(
    await audio.evaluate((element) => (element as HTMLAudioElement).duration),
  ).toBeCloseTo(0.1, 3);
  const before = await f.call("get_project_audio", {
    project_id: f.project.id,
  });
  expect(before.timeline.clips).toEqual([]);
  await page.screenshot({
    path: info.outputPath("speech-desktop.png"),
    fullPage: true,
  });
  await f.form.screenshot({ path: info.outputPath("speech-form.png") });

  // Native selects retain platform keyboard behavior, including the open popup.
  await supplier.focus();
  await supplier.press("Space");
  await supplier.press("Escape");
  await expect(supplier).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    (await new AxeBuilder({ page }).include(".speech-generator").analyze())
      .violations,
  ).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("speech-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "添加 配音测试 到音轨" }).click();
  await page.getByRole("button", { name: /^时间线/ }).click();
  await expect(page.getByLabel("增益（dB）", { exact: true })).toHaveValue("0");
  const unsaved = await f.call("get_project_audio", {
    project_id: f.project.id,
  });
  expect(unsaved.timeline.clips).toEqual([]);
  await page.getByRole("button", { name: "保存音轨", exact: true }).click();
  await expect(
    page.getByText("音轨已保存，将用于此项目的新交付任务。", { exact: true }),
  ).toBeVisible();
  const saved = await f.call("get_project_audio", { project_id: f.project.id });
  expect(saved.timeline.clips[0].asset_id).toBe(f.asset.id);
});

test("uncertain speech uses read-only recovery and preserves edited drafts", async ({
  page,
}) => {
  const f = await fixture(page);
  await f.form.getByLabel("配音供应商", { exact: true }).selectOption("openai");
  await f.form.getByLabel("配音台词", { exact: true }).fill("待确认的台词");
  await expect(
    f.form.getByRole("button", { name: "生成配音", exact: true }),
  ).toBeEnabled();
  await page.route("**/api/operations/generate_speech", async (route) => {
    f.calls.push({
      operation: "generate_speech",
      data: route.request().postDataJSON(),
    });
    await route.fulfill({
      status: 503,
      json: {
        error: { code: "speech_network", message: "连接中断，结果尚未确认" },
      },
    });
  });
  const statuses: string[] = [];
  await page.route("**/api/operations/get_speech_request", async (route) => {
    const data = route.request().postDataJSON();
    expect(data.idempotency_key).toBe(f.calls[0].data.idempotency_key);
    statuses.push(data.idempotency_key);
    await route.fulfill({
      json:
        statuses.length === 1
          ? { status: "pending", request_id: "test-request" }
          : { status: "complete", asset: f.asset, request_id: "test-request" },
    });
  });
  await f.form.getByRole("button", { name: "生成配音", exact: true }).click();
  await page
    .getByRole("dialog", { name: "确认生成云端配音" })
    .getByRole("button", { name: "确认生成并可能付费" })
    .click();
  await expect(f.form.getByRole("alert")).toContainText("连接中断");
  await f.form
    .getByLabel("配音台词", { exact: true })
    .fill("下一版的台词，保留此草稿");
  await f.form.getByRole("button", { name: "检查本次结果" }).click();
  await expect(
    f.form.getByText("本次请求仍在处理中。可稍后再次检查结果，不会重新生成。", {
      exact: true,
    }),
  ).toBeVisible();
  await f.form.getByRole("button", { name: "检查本次结果" }).click();
  await expect(
    f.form.getByText("AI 生成配音已加入素材，可试听并添加到音轨。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(f.form.getByLabel("配音台词", { exact: true })).toHaveValue(
    "下一版的台词，保留此草稿",
  );
  expect(f.calls).toHaveLength(1);
  expect(statuses).toHaveLength(2);
  await f.form.getByRole("button", { name: "生成新一版配音" }).click();
  await expect(
    page.getByRole("dialog", { name: "确认生成云端配音" }),
  ).toBeVisible();
  await page
    .getByRole("dialog", { name: "确认生成云端配音" })
    .getByRole("button", { name: "取消", exact: true })
    .click();
  expect(f.calls).toHaveLength(1);
});

test("speech config errors, stale reads and project guards retain drafts", async ({
  page,
}) => {
  const f = await fixture(page);
  f.setConfigured(false);
  await f.form.getByLabel("配音供应商", { exact: true }).selectOption("openai");
  await expect(f.form.getByText("密钥未配置", { exact: true })).toBeVisible();
  await expect(
    f.form.getByRole("button", { name: "生成配音", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "生成音频", exact: true }),
  ).toBeEnabled();
  await f.form.getByLabel("配音台词", { exact: true }).fill("保留的配音草稿");
  f.setConfigFailure(true);
  await f.form.getByRole("button", { name: "重新读取配音配置" }).click();
  await expect(f.form.getByRole("alert")).toContainText("配音配置暂时不可用");
  await expect(f.form.getByLabel("配音台词", { exact: true })).toHaveValue(
    "保留的配音草稿",
  );
  f.setConfigFailure(false);
  f.setConfigured(true);
  await f.form.getByRole("button", { name: "重新读取配音配置" }).click();
  await expect(f.form.getByRole("alert")).toHaveCount(0);
  await expect(
    f.form.getByRole("button", { name: "生成配音", exact: true }),
  ).toBeEnabled();

  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let oldRead = false;
  await page.route("**/api/operations/get_speech_config", async (route) => {
    if (route.request().postDataJSON().provider !== "openai") {
      await route.fallback();
      return;
    }
    oldRead = true;
    await gate;
    const reply = await route.fetch();
    await route.fulfill({
      json: { ...(await reply.json()), model: "stale-model" },
    });
  });
  await f.form.getByRole("button", { name: "重新读取配音配置" }).click();
  await expect.poll(() => oldRead).toBe(true);
  await f.form
    .getByLabel("配音供应商", { exact: true })
    .selectOption("openrouter");
  await expect(f.form.getByLabel("配音模型", { exact: true })).toHaveValue(
    "google/gemini-3.8-flash-tts",
  );
  release();
  await expect(f.form.getByLabel("配音供应商", { exact: true })).toHaveValue(
    "openrouter",
  );
  await expect(f.form.getByLabel("配音模型", { exact: true })).toHaveValue(
    "google/gemini-3.8-flash-tts",
  );
  const { project: other } = await f.call("create_project", {
    name: `另一个配音项目 ${Date.now()}`,
  });
  // Reload the bounded project picker without discarding the retained sound draft.
  await page.getByRole("button", { name: "刷新作品列表", exact: true }).click();
  await page.getByLabel("当前项目", { exact: true }).selectOption(other.id);
  await page
    .getByRole("dialog", { name: "保留未保存修改" })
    .getByRole("button", { name: "继续编辑" })
    .click();
  await expect(f.form.getByLabel("配音台词", { exact: true })).toHaveValue(
    "保留的配音草稿",
  );
  expect(f.calls).toEqual([]);
});
