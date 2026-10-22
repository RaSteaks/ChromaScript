import { expect, test } from "@playwright/test";

// Keep the real renderer and media decoder; only throttle frame notifications,
// as a busy/slow preview can do. Sound must not stop at the old 500 ms watchdog.
test("continuous audio survives sparse video updates without repeated seeks", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const observations = { pauses: 0, seeks: 0 };
    const seeks: unknown[] = [];
    const contexts: AudioContext[] = [];
    Object.assign(window, { seekTrace: seeks });
    const OriginalContext = window.AudioContext;
    window.AudioContext = class extends OriginalContext {
      constructor() {
        super();
        contexts.push(this);
      }
    };
    const currentTime = Object.getOwnPropertyDescriptor(
      HTMLMediaElement.prototype,
      "currentTime",
    )!;
    Object.defineProperty(HTMLMediaElement.prototype, "currentTime", {
      ...currentTime,
      set(value: number) {
        if (this instanceof HTMLAudioElement && this.closest(".preview-audio"))
          seeks.push({
            from: currentTime.get!.call(this),
            to: value,
            ready: this.readyState,
            seeking: this.seeking,
            clock: contexts[0]?.currentTime,
            time: performance.now(),
          });
        currentTime.set!.call(this, value);
      },
    });
    Object.assign(window, { observations, sparseFrames: false });
    for (const type of ["pause", "seeking"])
      document.addEventListener(
        type,
        (event) => {
          if (
            event.target instanceof HTMLAudioElement &&
            event.target.closest(".preview-audio")
          ) {
            const counts = (
              window as unknown as { observations: typeof observations }
            ).observations;
            if (type === "pause") counts.pauses++;
            else counts.seeks++;
          }
        },
        true,
      );
    let pending: MessageEvent | null = null;
    let timer = 0;
    window.addEventListener("message", (event) => {
      if (
        !event.isTrusted ||
        event.data?.type !== "frame" ||
        !(window as unknown as { sparseFrames: boolean }).sparseFrames
      )
        return;
      event.stopImmediatePropagation();
      pending = event;
      if (!timer)
        timer = window.setTimeout(() => {
          const message = pending!;
          timer = 0;
          window.dispatchEvent(
            new MessageEvent("message", {
              data: message.data,
              source: message.source,
            }),
          );
        }, 750);
    });
  });
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
    name: `音频连续性 ${Date.now()}`,
  });
  const { asset } = await call(
    process.env.CHROMASCRIPT_TEST_AUDIO_SOURCE
      ? "import_audio"
      : "create_audio",
    process.env.CHROMASCRIPT_TEST_AUDIO_SOURCE
      ? {
          project_id: project.id,
          name: "实际配乐验证",
          source_path: process.env.CHROMASCRIPT_TEST_AUDIO_SOURCE,
        }
      : {
          project_id: project.id,
          name: "持续音",
          spec: {
            duration_seconds: 20,
            events: [{ duration_seconds: 20, gain_db: -18 }],
          },
        },
  );
  await call("set_project_audio", {
    project_id: project.id,
    expected_revision_id: "0",
    timeline: { clips: [{ asset_id: asset.id }] },
  });
  const source = await call("create_motion_scene", { project_id: project.id });
  await call("update_motion_scene", {
    scene_id: source.record.id,
    expected_revision_id: source.revision_id,
    spec: {
      ...source.scene,
      width: 320,
      height: 180,
      frames: 600,
      fps: { numerator: 30, denominator: 1 },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("当前作品", { exact: true })
    .selectOption(`motion:${source.record.id}`);
  await expect(page.locator("iframe[title='动画画面']")).toBeVisible({
    timeout: 20000,
  });
  const audio = page.locator(".preview-audio:visible audio");
  await expect
    .poll(() => audio.evaluate((a: HTMLAudioElement) => a.readyState))
    .toBeGreaterThan(1);
  await page.getByRole("button", { name: "播放动画", exact: true }).click();
  await expect
    .poll(() => audio.evaluate((a: HTMLAudioElement) => a.currentTime))
    .toBeGreaterThan(0.5);
  await page.evaluate(() =>
    Object.assign(window, {
      sparseFrames: true,
      observations: { pauses: 0, seeks: 0 },
    }),
  );
  // Use media time, not a sleeping UI assertion, to observe several sparse-frame cycles.
  const start = await audio.evaluate((a: HTMLAudioElement) => a.currentTime);
  await expect
    .poll(() => audio.evaluate((a: HTMLAudioElement) => a.currentTime), {
      timeout: 7000,
    })
    .toBeGreaterThan(start + 3);
  const counts = await page.evaluate(
    () =>
      (window as unknown as { observations: { pauses: number; seeks: number } })
        .observations,
  );
  expect(counts.pauses).toBe(0);
  expect(
    counts.seeks,
    JSON.stringify(
      await page.evaluate(
        () => (window as unknown as { seekTrace: unknown[] }).seekTrace,
      ),
    ),
  ).toBe(0);
  await page.evaluate(() => Object.assign(window, { sparseFrames: false }));
  await page.getByRole("button", { name: "暂停动画", exact: true }).click();
  await expect
    .poll(() => audio.evaluate((a: HTMLAudioElement) => a.paused))
    .toBe(true);
  await page.getByLabel("动画时间线", { exact: true }).fill("300");
  await page.getByRole("button", { name: "播放动画", exact: true }).click();
  await expect
    .poll(() => audio.evaluate((a: HTMLAudioElement) => a.currentTime))
    .toBeGreaterThan(9.9);
});
