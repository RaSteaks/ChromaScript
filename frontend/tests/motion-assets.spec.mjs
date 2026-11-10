import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { compileFloatDocument, loadFloatDocument } from "../motion/runtime.mjs";

const fixture = JSON.parse(execFileSync("./.venv/bin/python", ["-c", `
import json
from pathlib import Path
from chromascript.motion import MotionSpec
from chromascript.motion_assets import import_gltf
spec = MotionSpec(width=64, height=64, frames=31, fps={"numerator":30,"denominator":1},
    assets={"horse":import_gltf(Path("tests/fixtures/motion-assets/triangle.gltf"))}, code='''
import {Scene, OrthographicCamera, AnimationMixer} from "three";
import {loadGLTF} from "chromascript:assets";
export default async function() {
  const model = await loadGLTF("horse");
  const scene = new Scene(), camera = new OrthographicCamera(-1,1,1,-1,0.1,10);
  camera.position.z = 2;
  scene.add(model.scene);
  const mixer = new AnimationMixer(model.scene);
  mixer.clipAction(model.animations[0]).play();
  return {scene, camera, update({time}) {mixer.setTime(time);}};
}
''')
print(json.dumps({"spec":spec.model_dump(),"display":spec.display()}))
`], { cwd: "..", encoding: "utf8", maxBuffer: 8 * 1024 ** 2 }));

async function operations(page) {
  const session = await (await page.request.get("/api/session")).json();
  return async (name, data) => {
    const response = await page.request.post(`/api/operations/${name}`, {
      headers: { Authorization: `Bearer ${session.token}` }, data,
    });
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  };
}

test("embedded glTF renders textured float frames and deterministic animation offline", async ({ page }, info) => {
  const compiled = await compileFloatDocument(fixture, "frames");
  const requests = [];
  await page.route("**/*", (route) => {
    requests.push(route.request().url());
    return route.abort();
  });
  await loadFloatDocument({
    goto: ({ url, timeout }) => page.goto(url, { timeout }),
    evaluate: (...args) => page.evaluate(...args),
  }, compiled.html);
  await page.waitForFunction(() => window.__chromaFloat || window.__chromaError);
  expect(await page.evaluate(() => window.__chromaError)).toBeUndefined();
  const pixels = await page.evaluate(() => [0, 15, 0].map((frame) => {
    window.__chromaFloat.capture(frame);
    return window.__chromaFloat.readChunk(0, 64 * 64 * 16);
  }));
  expect(pixels[0]).toBe(pixels[2]);
  expect(pixels[0]).not.toBe(pixels[1]);
  const values = Buffer.from(pixels[0], "base64");
  const center = (32 * 64 + 32) * 16;
  const rgba = Array.from({ length: 4 }, (_, i) => values.readFloatLE(center + i * 4));
  // Float storage associates decoded linear RGB once; texture alpha stays linear.
  const linearRGB = [0.6307571363, 0.0512694584, 0.0144438436];
  for (let i = 0; i < 3; i++) expect(rgba[i]).toBeCloseTo(linearRGB[i] * 128 / 255, 3);
  expect(rgba[3]).toBeCloseTo(128 / 255, 3);
  expect(await page.evaluate(async () => {
    try { await fetch("https://example.invalid/must-not-load"); return false; }
    catch { return true; }
  })).toBe(true);
  expect(requests).toEqual([]);
  await info.attach("float-resource-evidence", {
    body: JSON.stringify({ rgba, requests, repeatableSeeking: pixels[0] === pixels[2] }),
    contentType: "application/json",
  });
});

const sdrCode = `
import {ThreeCanvas} from "@remotion/three";
import {getGLTF} from "chromascript:assets";
import {useCurrentFrame, useVideoConfig} from "remotion";
export default function Animation() {
  const {width, height} = useVideoConfig();
  const frame = useCurrentFrame();
  return <ThreeCanvas width={width} height={height} camera={{position:[0,0,2],fov:50}}>
    <group position={[frame/300,0,0]}><primitive object={getGLTF("horse").scene} /></group>
  </ThreeCanvas>;
}`;

test("SDR loads frozen glTF before ready and editor saves retain the asset", async ({ page }, info) => {
  const call = await operations(page);
  const { project } = await call("create_project", { name: "local glTF browser test" });
  const initial = await call("create_motion_scene", {
    project_id: project.id,
    spec: { ...fixture.spec, engine: "remotion-sdr-v1", code: sdrCode, assets: {}, width: 320, height: 240 },
  });
  const imported = await call("import_motion_gltf", {
    scene_id: initial.record.id, expected_revision_id: initial.revision_id, name: "horse",
    path: fileURLToPath(new URL("../../tests/fixtures/motion-assets/triangle.gltf", import.meta.url)),
  });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page.getByLabel("当前作品", { exact: true }).selectOption(`motion:${imported.record.id}`);
  const picture = page.frameLocator("iframe[title='动画画面']");
  await expect(picture.locator("canvas")).toBeVisible({ timeout: 30000 });
  await page.screenshot({ path: info.outputPath("local-gltf-sdr.png") });
  const canvas = await picture.locator("canvas").screenshot();
  await info.attach("textured-model", { body: canvas, contentType: "image/png" });
  const editor = page.getByRole("textbox", { name: "动画 TypeScript 代码", exact: true });
  await editor.fill(sdrCode + "\n// Save must preserve revision-owned models.\n");
  await page.getByRole("button", { name: "保存作品", exact: true }).click();
  await expect(page.getByText("动画已保存。", { exact: true })).toBeVisible();
  const saved = await call("get_motion_scene", { scene_id: imported.record.id });
  expect(saved.scene.assets).toEqual(imported.scene.assets);
  expect(saved.revision_id).not.toBe(imported.revision_id);
});

test("an undecodable embedded texture rejects SDR startup instead of a textureless model", async ({ page }) => {
  const call = await operations(page);
  const document = JSON.parse(fixture.spec.assets.horse);
  document.images[0].uri = "data:image/png;base64,YmFk";
  const compiled = await call("compile_motion_preview", {
    spec: { ...fixture.spec, engine: "remotion-sdr-v1", code: sdrCode, assets: { horse: JSON.stringify(document) } },
  });
  await page.goto("/health");
  await page.evaluate(({ html, channel }) => {
    window.assetEvents = [];
    window.addEventListener("message", (event) => {
      if (event.data?.channel === channel) window.assetEvents.push(event.data);
    });
    const iframe = document.createElement("iframe");
    iframe.sandbox = "allow-scripts";
    iframe.srcdoc = html;
    document.body.appendChild(iframe);
  }, compiled);
  await expect.poll(() => page.evaluate(() => window.assetEvents.some((event) => event.type === "error"))).toBe(true);
  expect(await page.evaluate(() => window.assetEvents.some((event) => event.type === "ready"))).toBe(false);
});

test("the production SDR export bundle loads assets and seeks frames in WebKit", async ({ page }, info) => {
  const spec = { ...fixture.spec, engine: "remotion-sdr-v1", code: sdrCode, width: 320, height: 240 };
  const { serveUrl } = JSON.parse(execFileSync("node", ["motion/sdr-runtime.mjs", "bundle"], {
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    input: JSON.stringify({ spec, directory: info.outputPath("export") }),
    encoding: "utf8", maxBuffer: 8 * 1024 ** 2,
  }));
  // Serve the actual Webpack output through an isolated test origin. No production
  // Chromium driver is involved; the initialization/seek protocol is unchanged.
  const bundleURL = new URL("/gltf-bundle/", process.env.CHROMASCRIPT_URL).href;
  await page.route(bundleURL + "**", async (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() || "index.html";
    await route.fulfill({
      body: readFileSync(join(serveUrl, name)),
      contentType: name.endsWith(".js") ? "text/javascript" : "text/html",
    });
  });
  await page.addInitScript(() => {
    window.process = { env: { NODE_ENV: "production" } };
    window.remotion_puppeteerTimeout = 15000;
    window.remotion_isMainTab = true;
    window.remotion_inputProps = "{}";
    window.remotion_initialFrame = 0;
    window.remotion_attempt = 1;
    window.remotion_audioEnabled = false;
    window.remotion_videoEnabled = true;
    window.remotion_logLevel = "error";
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(bundleURL + "index.html");
  await page.waitForFunction(() => typeof window.remotion_setBundleMode === "function");
  await page.evaluate(() => window.remotion_setBundleMode({
    type: "composition", compositionName: "Animation", serializedResolvedPropsWithSchema: "{}",
    compositionDurationInFrames: 31, compositionFps: 30, compositionHeight: 240, compositionWidth: 320,
  }));
  const captures = [];
  for (const frame of [0, 15, 0]) {
    await page.waitForFunction(() => window.remotion_renderReady);
    await page.evaluate((frame) => window.remotion_setFrame(frame, "Animation", 1), frame);
    await page.waitForFunction(() => window.remotion_renderReady);
    captures.push(await page.locator("canvas").screenshot());
  }
  expect(captures[0].equals(captures[2])).toBe(true);
  expect(captures[0].equals(captures[1])).toBe(false);
  expect(errors).toEqual([]);
  await info.attach("export-frame-0", { body: captures[0], contentType: "image/png" });
  await info.attach("export-frame-15", { body: captures[1], contentType: "image/png" });
});
