import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// All viewers share one stable media tree, including the audio device clock.
// The repository browser configuration selects WebKit, never Google Chrome.
for (const engine of ["native", "three-float-v1", "remotion-sdr-v1"]) {
  test(`${engine} large preview preserves media, keyboard access and 720p detail`, async ({
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
      name: `大屏预览 ${engine} ${Date.now()}`,
    });
    const { asset } = await call("create_audio", {
      project_id: project.id,
      name: "连续监听",
      spec: {
        duration_seconds: 10,
        events: [{ duration_seconds: 10, gain_db: -30 }],
      },
    });
    await call("set_project_audio", {
      project_id: project.id,
      expected_revision_id: "0",
      timeline: { clips: [{ asset_id: asset.id }] },
    });
    const scene =
      engine === "native"
        ? await call("create_scene", {
            project_id: project.id,
            template_id: "gradient",
          })
        : await call("create_motion_scene", {
            project_id: project.id,
            spec: {
              name: "大屏画面",
              engine,
              width: 1920,
              height: 1080,
              frames: 300,
              fps: { numerator: 30, denominator: 1 },
              code:
                engine === "three-float-v1"
                  ? "import * as THREE from 'three'; export default function() { const scene = new THREE.Scene(); const camera = new THREE.PerspectiveCamera(45,16/9,.1,100); camera.position.z=4; scene.add(new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial({color:0x428d79}))); return {scene,camera,update({time}) {scene.rotation.y=time*.2;}}; }"
                  : "import {useCurrentFrame} from 'remotion'; export default function Scene() { return <div style={{width:'100%',height:'100%',background:'#203b42',color:'white',fontSize:100,display:'grid',placeContent:'center'}}>大屏预览 · {useCurrentFrame()}</div>; }",
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
      await expect
        .poll(() =>
          page
            .locator(".live-preview-image")
            .evaluate((img: HTMLImageElement) => [
              img.naturalWidth,
              img.naturalHeight,
            ]),
        )
        .toEqual([1280, 720]);
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
      if (engine === "three-float-v1") {
        await expect
          .poll(() =>
            page
              .frameLocator("iframe[title='动画画面']")
              .locator("canvas")
              .evaluate((canvas: HTMLCanvasElement) => [
                canvas.width,
                canvas.height,
              ]),
          )
          .toEqual([1280, 720]);
      }
    }
    const panel = page.locator(".preview-panel:visible");
    const surface = panel.locator(
      engine === "native" ? ".preview-stage" : ".motion-preview-surface",
    );
    const media = panel.locator(
      engine === "native" ? ".live-preview-image" : "iframe[title='动画画面']",
    );
    await media.evaluate((node) =>
      node.setAttribute("data-retained-media", "yes"),
    );
    const audio = panel.locator("audio").first();
    await expect
      .poll(() => audio.evaluate((node: HTMLAudioElement) => node.readyState))
      .toBeGreaterThan(0);
    await audio.evaluate((node) =>
      node.setAttribute("data-retained-audio", "yes"),
    );
    const playName = engine === "native" ? "播放实时预览" : "播放动画";
    const pauseName = engine === "native" ? "暂停实时预览" : "暂停动画";
    await panel.getByRole("button", { name: playName, exact: true }).click();
    await expect
      .poll(() =>
        audio.evaluate(
          (node: HTMLAudioElement) => !node.paused && node.currentTime > 0.2,
        ),
      )
      .toBe(true);
    const inline = await surface.boundingBox();
    await panel.getByRole("button", { name: "大屏查看", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: /大屏查看/ });
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: "退出大屏", exact: true }),
    ).toBeFocused();
    expect((await surface.boundingBox())!.width).toBeGreaterThan(
      inline!.width + 100,
    );
    await expect(media).toHaveAttribute("data-retained-media", "yes");
    await expect(audio).toHaveAttribute("data-retained-audio", "yes");
    await expect(
      panel.getByRole("button", { name: pauseName, exact: true }),
    ).toBeVisible();
    expect(await audio.evaluate((node: HTMLAudioElement) => node.paused)).toBe(
      false,
    );
    await page.screenshot({
      path: `/private/tmp/chromascript-large-${engine}.png`,
    });

    // Tab cannot escape to the inert editor; the sandbox forwards Escape too.
    await page.keyboard.press("Shift+Tab");
    expect(
      await dialog.evaluate((node) => node.contains(document.activeElement)),
    ).toBe(true);
    if (engine !== "native")
      await page
        .frameLocator("iframe[title='动画画面']")
        .locator("body")
        .click({ position: { x: 10, y: 10 } });
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(
      panel.getByRole("button", { name: "大屏查看", exact: true }),
    ).toBeFocused();
    await expect(media).toHaveAttribute("data-retained-media", "yes");
    await expect(audio).toHaveAttribute("data-retained-audio", "yes");
    await panel.getByRole("button", { name: pauseName, exact: true }).click();

    await page.setViewportSize({ width: 390, height: 844 });
    await panel.getByRole("button", { name: "大屏查看", exact: true }).click();
    await expect(
      dialog.getByRole("button", { name: "退出大屏", exact: true }),
    ).toBeInViewport();
    expect(
      await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
    ).toBe(true);
    const audit = await new AxeBuilder({ page })
      .include(".preview-panel.is-expanded")
      .analyze();
    expect(audit.violations).toEqual([]);
    await page.screenshot({
      path: `/private/tmp/chromascript-large-mobile-${engine}.png`,
    });
    await dialog.getByRole("button", { name: "退出大屏", exact: true }).click();
    expect(
      await page.evaluate(() => document.documentElement.style.overflow),
    ).not.toBe("hidden");
    const saved = await call(
      engine === "native" ? "get_scene" : "get_motion_scene",
      { scene_id: scene.record.id },
    );
    expect(saved.revision_id).toBe(scene.revision_id);
  });
}
