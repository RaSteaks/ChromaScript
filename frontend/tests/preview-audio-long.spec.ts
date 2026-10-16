import { expect, test } from "@playwright/test";

// Opt-in, read-only verification against an existing long soundtrack. No source,
// scene revision or audio timeline is written by this diagnostic.
test("existing long WAV soundtrack plays continuously and crosses its clip boundary", async ({
  page,
}) => {
  const projectId = process.env.CHROMASCRIPT_AUDIO_PROJECT;
  test.skip(
    !projectId,
    "Set CHROMASCRIPT_AUDIO_PROJECT to inspect an existing soundtrack",
  );
  test.setTimeout(90000);
  await page.addInitScript(() => {
    Object.assign(window, {
      audioEvents: [] as { type: string; src: string }[],
    });
    for (const type of ["pause", "seeking", "waiting"])
      document.addEventListener(
        type,
        (event) => {
          if (
            event.target instanceof HTMLAudioElement &&
            event.target.closest(".preview-audio")
          ) {
            (
              window as unknown as {
                audioEvents: { type: string; src: string }[];
              }
            ).audioEvents.push({ type, src: event.target.currentSrc });
          }
        },
        true,
      );
  });
  const session = await (await page.request.get("/api/session")).json();
  async function read(name: string, data: unknown) {
    const response = await page.request.post(`/api/operations/${name}`, {
      headers: { Authorization: `Bearer ${session.token}` },
      data,
    });
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  }
  const before = await read("get_project_audio", { project_id: projectId });
  const scenes = await read("list_motion_scenes", { project_id: projectId });
  const scene = await read("get_motion_scene", {
    scene_id: scenes.items[0].id,
  });
  const fps = scene.scene.fps.numerator / scene.scene.fps.denominator;
  const boundary = before.timeline.clips[1].start_seconds;
  expect(boundary).toBeGreaterThan(5);
  await page.goto("/");
  await page.getByRole("button", { name: "动画代码", exact: true }).click();
  await page.getByLabel("动画项目", { exact: true }).selectOption(projectId!);
  await page
    .getByLabel("已保存的动画", { exact: true })
    .selectOption(scene.record.id);
  await expect(page.locator("iframe[title='动画画面']")).toBeVisible({
    timeout: 50000,
  });
  const audio = page.locator(".preview-audio:visible audio");
  await expect(audio).toHaveCount(before.timeline.clips.length);
  const first = audio.nth(0),
    second = audio.nth(1);
  await page
    .getByLabel("动画时间线", { exact: true })
    .fill(String(Math.round(20 * fps)));
  await page.getByRole("button", { name: "播放动画", exact: true }).click();
  await expect
    .poll(() => first.evaluate((a: HTMLAudioElement) => a.currentTime), {
      timeout: 15000,
    })
    .toBeGreaterThan(20.5);
  await page.evaluate(() => Object.assign(window, { audioEvents: [] }));
  const start = await first.evaluate((a: HTMLAudioElement) => a.currentTime);
  await expect
    .poll(() => first.evaluate((a: HTMLAudioElement) => a.currentTime), {
      timeout: 10000,
    })
    .toBeGreaterThan(start + 4);
  const events = await page.evaluate(
    () =>
      (window as unknown as { audioEvents: { type: string; src: string }[] })
        .audioEvents,
  );
  expect(events.filter((event) => event.type === "pause")).toHaveLength(0);
  expect(events.filter((event) => event.type === "seeking")).toHaveLength(0);
  await page.getByRole("button", { name: "暂停动画", exact: true }).click();
  await page
    .getByLabel("动画时间线", { exact: true })
    .fill(String(Math.round((boundary - 1) * fps)));
  await page.getByRole("button", { name: "播放动画", exact: true }).click();
  await expect
    .poll(() => first.evaluate((a: HTMLAudioElement) => a.currentTime))
    .toBeGreaterThan(boundary - 1.1);
  await expect
    .poll(
      () =>
        second.evaluate(
          (a: HTMLAudioElement) => !a.paused && a.currentTime > 1,
        ),
      { timeout: 10000 },
    )
    .toBe(true);
  expect(await first.evaluate((a: HTMLAudioElement) => a.paused)).toBe(true);
  await page.evaluate(() => Object.assign(window, { audioEvents: [] }));
  const secondStart = await second.evaluate(
    (a: HTMLAudioElement) => a.currentTime,
  );
  await expect
    .poll(() => second.evaluate((a: HTMLAudioElement) => a.currentTime), {
      timeout: 10000,
    })
    .toBeGreaterThan(secondStart + 3);
  const afterBoundary = await page.evaluate(
    () =>
      (window as unknown as { audioEvents: { type: string; src: string }[] })
        .audioEvents,
  );
  expect(afterBoundary.filter((event) => event.type === "pause")).toHaveLength(
    0,
  );
  expect(
    afterBoundary.filter((event) => event.type === "seeking"),
  ).toHaveLength(0);
  await page.getByRole("button", { name: "暂停动画", exact: true }).click();
  expect(
    (await read("get_project_audio", { project_id: projectId })).revision_id,
  ).toBe(before.revision_id);
  expect(
    (await read("get_motion_scene", { scene_id: scene.record.id })).revision_id,
  ).toBe(scene.revision_id);
});
