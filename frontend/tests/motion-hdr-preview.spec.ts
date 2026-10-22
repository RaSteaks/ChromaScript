import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";
import { execFileSync } from "node:child_process";

// Keep display-query fixtures independent of the host monitor while exercising
// WebKit's real HEVC decoder. These tests do not measure physical HDR luminance.
async function hdrScreen(page: Page, safariFallback = false) {
  await page.addInitScript((fallback) => {
    const original = window.matchMedia.bind(window);
    window.matchMedia = (query) => {
      const result = original(query);
      if (
        query === "(video-dynamic-range: high)" ||
        (fallback && query === "(video-dynamic-range: standard)")
      )
        Object.defineProperty(result, "matches", { value: !fallback });
      if (fallback && query === "(dynamic-range: high)")
        Object.defineProperty(result, "matches", { value: true });
      return result;
    };
  }, safariFallback);
}

async function setup(page: Page) {
  const { token } = await (await page.request.get("/api/session")).json();
  const call = async (name: string, data: unknown) => {
    const response = await page.request.post(`/api/operations/${name}`, {
      headers: { Authorization: `Bearer ${token}` },
      data,
    });
    expect(response.ok(), await response.text()).toBeTruthy();
    return response.json();
  };
  const { project } = await call("create_project", {
    name: `HDR preview ${Date.now()}`,
  });
  const scene = await call("create_motion_scene", {
    project_id: project.id,
    spec: {
      name: "HDR 低分辨率验证",
      engine: "three-float-v1",
      source_color_space: "Linear Rec.2020",
      width: 1920,
      height: 1080,
      frames: 240,
      fps: { numerator: 60000, denominator: 1001 },
      code: `import * as THREE from 'three';
// Scene-linear highlights move deterministically at the original rational time.
export default function createScene() {
  const scene=new THREE.Scene(),camera=new THREE.Camera();
  const material=new THREE.ShaderMaterial({depthTest:false,toneMapped:false,
    uniforms:{phase:{value:0}},
    vertexShader:'varying vec2 uv0;void main(){uv0=uv;gl_Position=vec4(position.xy,0.,1.);}',
    fragmentShader:'varying vec2 uv0;uniform float phase;void main(){float g=exp(-30.*pow(uv0.x-phase,2.));gl_FragColor=vec4(vec3(.1+g*12.),1.);}'});
  scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
  return {scene,camera,update({time}){material.uniforms.phase.value=(time*.3)%1.;}};
}`,
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("当前作品", { exact: true })
    .selectOption(`motion:${scene.record.id}`);
  await expect(page.locator("iframe[title='动画画面']")).toBeVisible({
    timeout: 20000,
  });
  return page.frameLocator("iframe[title='动画画面']");
}

test("GPU P010 preserves one ACES PQ transform, black alpha compositing and row order", async ({
  page,
}) => {
  const expected = JSON.parse(
    execFileSync(
      "./.venv/bin/python",
      [
        "-c",
        `
import json,numpy as np
from chromascript.color import transform
out=[]
for value in (.125,8.0):
 rgb=transform(np.array([value,.2*value,.05*value],dtype=np.float32),"PQ",source="Linear Rec.2020")
 y=float(np.dot(rgb,[.2627,.6780,.0593]))
 out.append([round(64+876*y),round(512+896*(float(rgb[2])-y)/1.8814),round(512+896*(float(rgb[0])-y)/1.4746)])
print(json.dumps(out))
`,
      ],
      { encoding: "utf8", cwd: ".." },
    ),
  );
  const { token } = await (await page.request.get("/api/session")).json();
  const response = await page.request.post(
    "/api/operations/compile_motion_preview",
    {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        hdr_preview: true,
        spec: {
          source_color_space: "Linear Rec.2020",
          width: 32,
          height: 16,
          frames: 1,
          code: `import * as THREE from 'three';export default function createScene(){
const scene=new THREE.Scene(),camera=new THREE.Camera();
scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),new THREE.ShaderMaterial({
transparent:true,depthTest:false,toneMapped:false,
vertexShader:'void main(){gl_Position=vec4(position.xy,0.,1.);}',
fragmentShader:'void main(){float v=gl_FragCoord.y<8.?16.:.25;gl_FragColor=vec4(v,.2*v,.05*v,.5);}'
})));return {scene,camera,update(){}};}`,
        },
      },
    },
  );
  expect(response.ok(), await response.text()).toBeTruthy();
  const { html } = await response.json();
  await page.goto("/");
  await page.evaluate((html) => {
    document.body.innerHTML = '<iframe sandbox="allow-scripts"></iframe>';
    document.querySelector("iframe")!.srcdoc = html;
  }, html);
  const frame = page.frames().find((item) => item.parentFrame())!;
  await frame.waitForFunction(
    () => !!(window as any).__chromaFloat || !!(window as any).__chromaError,
  );
  expect(
    await frame.evaluate(() => (window as any).__chromaError),
  ).toBeUndefined();
  const samples = await frame.evaluate(() => {
    const values = new Uint16Array((window as any).__chromaFloat.captureHDR(0));
    return {
      top: values[4 * 32 + 16] >> 6,
      bottom: values[12 * 32 + 16] >> 6,
      topUV: Array.from(
        values.slice(32 * 16 + 2 * 32 + 16, 32 * 16 + 2 * 32 + 18),
      ).map((v) => v >> 6),
      bottomUV: Array.from(
        values.slice(32 * 16 + 6 * 32 + 16, 32 * 16 + 6 * 32 + 18),
      ).map((v) => v >> 6),
      lowBits: Array.from(values).some((v) => v & 63),
    };
  });
  expect(Math.abs(samples.top - expected[0][0])).toBeLessThanOrEqual(1);
  expect(Math.abs(samples.bottom - expected[1][0])).toBeLessThanOrEqual(1);
  for (let i = 0; i < 2; i++) {
    expect(Math.abs(samples.topUV[i] - expected[0][i + 1])).toBeLessThanOrEqual(
      1,
    );
    expect(
      Math.abs(samples.bottomUV[i] - expected[1][i + 1]),
    ).toBeLessThanOrEqual(1);
  }
  expect(samples.lowBits).toBe(false);
});

test("HDR video uses native decoded segments, bounds buffers, seeks and pauses", async ({
  page,
}, info) => {
  await hdrScreen(page, true);
  let encoded = 0;
  page.on("response", (response) => {
    if (
      response.url().endsWith("/encode_motion_hdr_preview/binary") &&
      response.ok()
    )
      encoded++;
  });
  const frame = await setup(page);
  await expect(page.getByText("ACES · HDR PQ", { exact: true })).toBeVisible({
    timeout: 30000,
  });
  const video = page.locator(".motion-preview-surface video");
  await expect(video).toBeVisible();
  expect(
    await video.evaluate((node: HTMLVideoElement) => ({
      width: node.videoWidth,
      height: node.videoHeight,
      paused: node.paused,
    })),
  ).toEqual({ width: 1280, height: 720, paused: true });
  const timeline = page.getByLabel("动画时间线", { exact: true });
  await page.getByRole("button", { name: "播放动画", exact: true }).click();
  await expect
    .poll(async () => Number(await timeline.inputValue()))
    .toBeGreaterThan(0);
  await expect.poll(() => encoded).toBeGreaterThanOrEqual(5);
  await expect
    .poll(() => video.evaluate((node: HTMLVideoElement) => node.currentTime), {
      timeout: 20000,
    })
    .toBeGreaterThan(4);
  const windowSize = await video.evaluate(
    (node: HTMLVideoElement) =>
      node.buffered.end(node.buffered.length - 1) - node.buffered.start(0),
  );
  expect(windowSize).toBeLessThan(8);
  await page.getByRole("button", { name: "暂停动画", exact: true }).click();
  await page.waitForTimeout(100);
  const paused = await timeline.inputValue();
  await page.waitForTimeout(400);
  await expect(timeline).toHaveValue(paused);
  await timeline.fill("120");
  await expect(timeline).toHaveValue("120");
  await expect(page.getByText("ACES · HDR PQ", { exact: true })).toBeVisible();
  await expect(video).toBeVisible();
  await page.waitForTimeout(300);
  await expect(timeline).toHaveValue("120");
  const evidence = await video.evaluate((node: HTMLVideoElement) => ({
    width: node.videoWidth,
    height: node.videoHeight,
    time: node.currentTime,
    ranges: Array.from({ length: node.buffered.length }, (_, i) => [
      node.buffered.start(i),
      node.buffered.end(i),
    ]),
  }));
  await info.attach("native-video-evidence", {
    body: JSON.stringify({ ...evidence, encoded, displayQuerySimulated: true }),
    contentType: "application/json",
  });
  mkdirSync("../reports", { recursive: true });
  await page
    .locator(".motion-preview-panel")
    .screenshot({ path: "../reports/hdr-preview-native-video.png" });
  await page.getByLabel("预览显示", { exact: true }).selectOption("srgb");
  await expect(page.getByText("ACES · sRGB", { exact: true })).toBeVisible();
  await expect(page.locator(".motion-preview-surface video")).toHaveCount(0);
});

test("automatic SDR fallback remains accessible on a non-HDR display", async ({
  page,
}) => {
  await page.addInitScript(() => {
    // Headless WebKit can inherit the host's general HDR display signal.
    // Explicitly model an SDR video plane rather than assuming one from headless.
    const original = window.matchMedia.bind(window);
    window.matchMedia = (query) => {
      const result = original(query);
      if (
        [
          "(video-dynamic-range: high)",
          "(video-dynamic-range: standard)",
          "(dynamic-range: high)",
        ].includes(query)
      )
        Object.defineProperty(result, "matches", {
          value: query === "(video-dynamic-range: standard)",
        });
      return result;
    };
  });
  const frame = await setup(page);
  await expect(page.getByText("ACES · sRGB", { exact: true })).toBeVisible();
  await expect(page.locator(".motion-preview-surface video")).toHaveCount(0);
  await page.getByLabel("预览显示", { exact: true }).focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByLabel("预览显示", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "播放动画", exact: true }),
  ).toBeVisible();
  const audit = await new AxeBuilder({ page })
    .include(".motion-preview-panel")
    .analyze();
  expect(audit.violations).toEqual([]);
  await page
    .locator(".motion-preview-panel")
    .screenshot({ path: "../reports/hdr-preview-mobile-fallback.png" });
});

test("encoder failure preserves the canvas, source and playback recovery", async ({
  page,
}) => {
  await hdrScreen(page);
  await page.route("**/encode_motion_hdr_preview/binary", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: { message: "测试编码器不可用" } }),
    }),
  );
  await setup(page);
  await expect(page.getByText(/测试编码器不可用.*已回退 sRGB/)).toBeVisible();
  await expect(page.getByText("ACES · sRGB", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "播放动画", exact: true }).click();
  await expect
    .poll(async () =>
      Number(await page.getByLabel("动画时间线", { exact: true }).inputValue()),
    )
    .toBeGreaterThan(0);
});

test("late fragments cannot replace an explicitly selected SDR preview", async ({
  page,
}) => {
  await hdrScreen(page);
  let started = false;
  await page.route("**/encode_motion_hdr_preview/binary", async (route) => {
    started = true;
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await route
      .fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: { message: "obsolete fragment" } }),
      })
      .catch(() => {});
  });
  const frame = await setup(page);
  await expect.poll(() => started).toBe(true);
  await page.getByLabel("预览显示", { exact: true }).selectOption("srgb");
  await expect(page.locator(".motion-preview-surface video")).toHaveCount(0);
  await page.waitForTimeout(1500);
  await expect(page.getByText("ACES · sRGB", { exact: true })).toBeVisible();
  await expect(page.getByText(/obsolete fragment/)).toHaveCount(0);
});

test("HDR clock recovers after seeking without video frame callbacks", async ({
  page,
}) => {
  await hdrScreen(page);
  await page.addInitScript(() => {
    // Exercise the real native player on older API surfaces, without replacing
    // decoded pixels or the media clock with a test implementation.
    Object.defineProperty(
      HTMLVideoElement.prototype,
      "requestVideoFrameCallback",
      {
        value: undefined,
      },
    );
  });
  await setup(page);
  await expect(page.getByText("ACES · HDR PQ", { exact: true })).toBeVisible({
    timeout: 30000,
  });
  const timeline = page.getByLabel("动画时间线", { exact: true });
  await page.getByRole("button", { name: "播放动画", exact: true }).click();
  await expect
    .poll(async () => Number(await timeline.inputValue()))
    .toBeGreaterThan(3);
  await timeline.fill("120");
  await page.getByRole("button", { name: "播放动画", exact: true }).click();
  await expect
    .poll(async () => Number(await timeline.inputValue()), { timeout: 20000 })
    .toBeGreaterThan(123);
  await page.getByRole("button", { name: "暂停动画", exact: true }).click();
  await page.waitForTimeout(100);
  const stopped = await timeline.inputValue();
  await page.waitForTimeout(250);
  await expect(timeline).toHaveValue(stopped);
});
