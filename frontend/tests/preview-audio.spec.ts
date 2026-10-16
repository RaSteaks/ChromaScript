import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const engine of ["native", "three-float-v1", "remotion-sdr-v1"]) {
  test(`${engine} preview synchronizes real project PCM and agent saves`, async ({
    page,
  }) => {
    // Tap the real Web Audio graph, not mocked play() calls: nonzero samples
    // prove media decoding and the gain path actually reach the output graph.
    await page.addInitScript(() => {
      const NativeContext = window.AudioContext;
      const taps: AnalyserNode[] = [];
      const gainNodes: GainNode[] = [];
      Object.assign(window, { audioTaps: taps, audioGains: gainNodes });
      window.AudioContext = class extends NativeContext {
        createGain() {
          const gain = super.createGain();
          const analyser = this.createAnalyser();
          gain.connect(analyser);
          taps.push(analyser);
          gainNodes.push(gain);
          return gain;
        }
      };
    });
    const session = await (await page.request.get("/api/session")).json();
    async function call(name: string, data: unknown) {
      const result = await page.request.post(`/api/operations/${name}`, {
        headers: { Authorization: `Bearer ${session.token}` },
        data,
      });
      expect(result.ok(), await result.text()).toBe(true);
      return result.json();
    }
    const { project } = await call("create_project", {
      name: `预览声音 ${engine} ${Date.now()}`,
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
            name: "同步正弦波",
            spec: {
              duration_seconds: 6,
              events: [
                { duration_seconds: 6, frequency_hz: 440, gain_db: -12 },
              ],
            },
          },
    );
    const clips = [
      {
        asset_id: asset.id,
        start_seconds: 0,
        source_start_seconds: 0.4,
        duration_seconds: 4.5,
        gain_db: -6,
      },
      {
        asset_id: asset.id,
        start_seconds: 1,
        source_start_seconds: 0,
        duration_seconds: 0.3,
        gain_db: 6,
      },
    ];
    const saved = await call("set_project_audio", {
      project_id: project.id,
      expected_revision_id: "0",
      timeline: { clips },
    });
    const scene =
      engine === "native"
        ? await call("create_scene", { project_id: project.id })
        : await call("create_motion_scene", {
            project_id: project.id,
            spec: {
              name: "音画同步",
              engine,
              width: 320,
              height: 180,
              frames: 150,
              fps: { numerator: 30000, denominator: 1001 },
              code:
                engine === "three-float-v1"
                  ? "import * as THREE from 'three'; export default function() { const scene = new THREE.Scene(); const camera = new THREE.PerspectiveCamera(); return { scene, camera, update() {} }; }"
                  : "export default function Scene() { return <div style={{background:'black',width:'100%',height:'100%'}} />; }",
            },
          });
    await page.goto("/");
    if (engine === "native") {
      await page
        .getByLabel("当前项目", { exact: true })
        .selectOption(project.id);
      await page
        .getByRole("button", { name: "继续编辑与预览", exact: true })
        .click();
      await expect(page.locator(".live-preview-image")).toBeVisible();
    } else {
      await page.getByRole("button", { name: "动画代码", exact: true }).click();
      await page
        .getByLabel("动画项目", { exact: true })
        .selectOption(project.id);
      await page
        .getByLabel("已保存的动画", { exact: true })
        .selectOption(scene.record.id);
      await expect(page.locator("iframe[title='动画画面']")).toBeVisible({
        timeout: 20000,
      });
    }
    const panel = page.locator(".preview-audio:visible");
    const audio = panel.locator("audio").first();
    await expect(panel.getByRole("slider", { name: "预览音量" })).toBeEnabled();
    await expect
      .poll(() => audio.evaluate((a: HTMLAudioElement) => a.readyState))
      .toBeGreaterThan(0);
    expect(await audio.evaluate((a: HTMLAudioElement) => a.paused)).toBe(true);
    const play = () =>
      page.getByRole("button", {
        name: engine === "native" ? "播放实时预览" : "播放动画",
        exact: true,
      });
    const pause = () =>
      page.getByRole("button", {
        name: engine === "native" ? "暂停实时预览" : "暂停动画",
        exact: true,
      });
    await play().click();
    await expect
      .poll(() =>
        audio.evaluate(
          (a: HTMLAudioElement) => !a.paused && a.currentTime > 0.4,
        ),
      )
      .toBe(true);
    await expect
      .poll(() =>
        page.evaluate(() => {
          const taps = (window as unknown as { audioTaps: AnalyserNode[] })
            .audioTaps;
          return taps.some((tap) => {
            const data = new Float32Array(tap.fftSize);
            tap.getFloatTimeDomainData(data);
            return data.some((value) => Math.abs(value) > 0.01);
          });
        }),
      )
      .toBe(true);
    expect(
      await page.evaluate(
        () =>
          (window as unknown as { audioGains: GainNode[] }).audioGains[1].gain
            .value,
      ),
    ).toBeCloseTo(10 ** (-6 / 20), 4);
    // Independently timed overlap and positive gain must survive browser volume limits.
    await expect
      .poll(
        () =>
          panel
            .locator("audio")
            .nth(1)
            .evaluate((a: HTMLAudioElement) => !a.paused),
        { intervals: [25, 25, 50] },
      )
      .toBe(true);
    expect(
      await page.evaluate(
        () =>
          (window as unknown as { audioGains: GainNode[] }).audioGains[2].gain
            .value,
      ),
    ).toBeCloseTo(10 ** (6 / 20), 4);
    await expect
      .poll(
        () =>
          panel
            .locator("audio")
            .nth(1)
            .evaluate((a: HTMLAudioElement) => a.paused),
        { intervals: [50] },
      )
      .toBe(true);
    await pause().click();
    await expect
      .poll(() => audio.evaluate((a: HTMLAudioElement) => a.paused))
      .toBe(true);
    const seek = page.getByLabel(
      engine === "native" ? "实时预览播放位置" : "动画时间线",
      { exact: true },
    );
    await seek.fill("60");
    await play().click();
    await expect
      .poll(() => audio.evaluate((a: HTMLAudioElement) => a.currentTime))
      .toBeGreaterThan(2.3);
    const volume = panel.getByRole("slider", { name: "预览音量" });
    const monitorGain = () =>
      page.evaluate(
        () =>
          (window as unknown as { audioGains: GainNode[] }).audioGains[0].gain
            .value,
      );
    // The monitor fader scales the output graph, without pausing/reseeking clips
    // or modifying project gain. Keyboard End/Home are native range actions.
    const beforeVolume = await audio.evaluate(
      (a: HTMLAudioElement) => a.currentTime,
    );
    await volume.fill("50");
    await expect.poll(monitorGain).toBeCloseTo(0.5, 3);
    await expect(panel.locator(".preview-audio-percentage")).toHaveText("50%");
    await volume.focus();
    await volume.press("Home");
    await expect(volume).toHaveValue("0");
    await expect(volume).toHaveAttribute("aria-valuetext", "静音，0%");
    await expect.poll(monitorGain).toBeCloseTo(0, 3);
    expect(await audio.evaluate((a: HTMLAudioElement) => a.paused)).toBe(false);
    expect(
      await audio.evaluate((a: HTMLAudioElement) => a.currentTime),
    ).toBeGreaterThan(beforeVolume);
    await volume.press("End");
    await expect.poll(monitorGain).toBeCloseTo(1, 3);
    const unchanged = await call("get_project_audio", {
      project_id: project.id,
    });
    expect(unchanged.revision_id).toBe(saved.revision_id);
    expect(unchanged.timeline.clips[0].gain_db).toBe(-6);
    await pause().click();

    await volume.fill("25");
    await expect.poll(monitorGain).toBeCloseTo(0.25, 3);
    expect(await audio.evaluate((a: HTMLAudioElement) => a.paused)).toBe(true);
    await volume.fill("100");

    // A loop must restart the source trim, including when the preceding tail is silent.
    await seek.fill(String(Number(await seek.getAttribute("max")) - 1));
    await play().click();
    await expect
      .poll(
        () =>
          audio.evaluate(
            (a: HTMLAudioElement) =>
              !a.paused && a.currentTime >= 0.4 && a.currentTime < 1,
          ),
        { intervals: [50] },
      )
      .toBe(true);
    await pause().click();

    if (engine === "native") {
      await page.route("**/api/operations/get_project_audio", (route) =>
        route.fulfill({
          status: 503,
          json: { error: { message: "音轨连接测试失败" } },
        }),
      );
      await expect(panel.getByRole("alert")).toContainText("音轨连接测试失败", {
        timeout: 6000,
      });
      await page.unroute("**/api/operations/get_project_audio");
      await panel.getByRole("button", { name: "重试预览音频" }).click();
      await expect(
        panel.getByRole("slider", { name: "预览音量" }),
      ).toBeEnabled();
      await expect(panel.getByRole("alert")).toHaveCount(0);
      // Missing/undecodable media is actionable and retry reloads the same asset.
      await page.route(`**/api/artifacts/${asset.id}`, (route) =>
        route.fulfill({ status: 404, body: "missing" }),
      );
      await audio.evaluate((node: HTMLAudioElement) => node.load());
      await expect(panel.getByRole("alert")).toContainText("无法读取或解码");
      await page.unroute(`**/api/artifacts/${asset.id}`);
      await panel.getByRole("button", { name: "重试预览音频" }).click();
      await expect
        .poll(() => audio.evaluate((node: HTMLAudioElement) => node.readyState))
        .toBeGreaterThan(0);
      await expect(panel.getByRole("alert")).toHaveCount(0);
    }
    // An agent's replacement is observed without reloading or opening the editor.
    const cleared = await call("set_project_audio", {
      project_id: project.id,
      expected_revision_id: saved.revision_id,
      timeline: { clips: [] },
    });
    await expect(panel.getByText("项目未设置音轨")).toBeVisible({
      timeout: 6000,
    });
    await expect(panel.locator("audio")).toHaveCount(0);
    await call("set_project_audio", {
      project_id: project.id,
      expected_revision_id: cleared.revision_id,
      timeline: { clips },
    });
    await expect(panel.locator("audio")).toHaveCount(2, { timeout: 6000 });
    await play().click();
    await expect
      .poll(() => audio.evaluate((a: HTMLAudioElement) => !a.paused))
      .toBe(true);
    // Hiding the document stops the real stream, even before renderer acknowledgement.
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect
      .poll(() => audio.evaluate((a: HTMLAudioElement) => a.paused))
      .toBe(true);
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: false,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect(play()).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    const audit = await new AxeBuilder({ page })
      .include(".preview-audio")
      .analyze();
    expect(audit.violations).toEqual([]);
    expect(
      await panel.evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
    ).toBe(true);
    await page.screenshot({
      path: `/private/tmp/chromascript-preview-audio-${engine}.png`,
      fullPage: true,
    });
  });
}
