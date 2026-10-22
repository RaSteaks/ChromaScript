import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";
import type { MotionReply, MotionSpec, MotionSourceSpace } from "../src/types";

// Compile through the real API, then run the shared export/preview bridge in
// headless WebKit. Never submit a Chrome-backed production render from this test.
async function api(page: Page) {
  const { token } = await (await page.request.get("/api/session")).json();
  return async <T>(name: string, args: unknown): Promise<T> => {
    const response = await page.request.post(`/api/operations/${name}`, {
      headers: { Authorization: `Bearer ${token}` },
      data: args,
    });
    expect(response.ok(), await response.text()).toBeTruthy();
    return response.json();
  };
}
const code = `import * as THREE from 'three';
// These execute at module load, before createScene: the host must set primaries first.
const numeric = new THREE.Color(0.2,0.3,0.4);
const css = new THREE.Color('#00ff00');
window.__authorColors = {numeric:numeric.toArray(),css:css.toArray()};
export default function createScene(){
  const scene=new THREE.Scene(), camera=new THREE.Camera();
  const material=new THREE.ShaderMaterial({transparent:true,depthTest:false,
    vertexShader:'void main(){gl_Position=vec4(position.xy,0.,1.);}',
    fragmentShader:'void main(){gl_FragColor=vec4(-0.25,0.1234567,4.125,0.5);}',toneMapped:false});
  scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
  return {scene,camera,update(){}};
}`;
async function mount(page: Page, spec: MotionSpec) {
  const call = await api(page);
  const compiled = await call<{ html: string }>("compile_motion_preview", {
    spec,
  });
  await page.goto("/");
  await page.evaluate((html) => {
    document.body.innerHTML =
      '<iframe sandbox="allow-scripts" style="width:128px;height:128px"></iframe>';
    document.querySelector("iframe")!.srcdoc = html;
  }, compiled.html);
  const frame = page.frames().find((item) => item.parentFrame());
  expect(frame).toBeTruthy();
  await frame!.waitForFunction(
    () => !!(window as any).__chromaFloat || !!(window as any).__chromaError,
  );
  expect(
    await frame!.evaluate(() => (window as any).__chromaError),
  ).toBeUndefined();
  return frame!;
}
async function capture(frame: Awaited<ReturnType<typeof mount>>, index = 0) {
  return frame.evaluate((index) => {
    const bridge = (window as any).__chromaFloat;
    const result = bridge.capture(index);
    const binary = atob(bridge.readChunk(0, result.bytes));
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    return {
      pixel: Array.from(new Float32Array(bytes.buffer).slice(0, 4)),
      probe: bridge.probe,
    };
  }, index);
}

test("shared renderer releases geometry from independent source graphs", async ({
  page,
}) => {
  const source = (
    name: string,
    padding: number,
  ) => `import * as THREE from 'three';
    // Count native GL calls, not Three's memory counter (which itself misses ID collisions).
    if (!window.__resourceStats) {
      window.__resourceStats = {created:0, deleted:0};
      window.__resourceObjects = {};
      const gl = WebGL2RenderingContext.prototype;
      const create = gl.createBuffer, remove = gl.deleteBuffer;
      gl.createBuffer = function(...args) {
        const buffer = create.apply(this,args);
        if (buffer) window.__resourceStats.created++;
        return buffer;
      };
      gl.deleteBuffer = function(buffer) {
        if (buffer) window.__resourceStats.deleted++;
        return remove.call(this,buffer);
      };
    }
    export default function createScene() {
      // Deliberately align the old module-local counters. This also covers a
      // collision with the host quad, whose geometry is allocated before factories.
      for (let i=0;i<${padding};i++) new THREE.PlaneGeometry(2,2).dispose();
      const scene = new THREE.Scene(), camera = new THREE.Camera();
      const material = new THREE.MeshBasicMaterial({color:0xffffff,toneMapped:false});
      const mesh = new THREE.Mesh(new THREE.PlaneGeometry(2,2),material);
      scene.add(mesh);
      window.__resourceObjects[${JSON.stringify(name)}] = mesh;
      return {scene,camera,update() {
        // Changing geometry is valid even when the picture depends only on frame.
        mesh.geometry.dispose();
        mesh.geometry = new THREE.PlaneGeometry(2,2);
      }};
    }`;
  const frame = await mount(page, {
    engine: "three-float-v1",
    source_color_space: "ACES2065-1",
    name: "resource lifecycle",
    code: source("native", 0),
    width: 16,
    height: 16,
    frames: 32,
    fps: { numerator: 30, denominator: 1 },
    preserved_layers: [
      {
        source_color_space: "Linear Rec.709 (sRGB)",
        code: source("legacy", 2),
      },
      { source_color_space: "ACEScg", code: source("ap1", 2) },
    ],
  });
  const evidence = await frame.evaluate(() => {
    const w = window as any;
    const samples = [];
    for (let index = 0; index < 24; index++) {
      w.__chromaFloat.capture(index);
      const objects = Object.values(w.__resourceObjects) as any[];
      samples.push({
        frame: index,
        ...w.__resourceStats,
        geometryIds: objects.map((mesh) => mesh.geometry.id),
        objectIds: objects.map((mesh) => mesh.id),
        materialIds: objects.map((mesh) => mesh.material.id),
        disposalListeners: objects.map(
          (mesh) => mesh.geometry._listeners?.dispose?.length ?? 0,
        ),
      });
    }
    const before = { ...w.__resourceStats };
    for (const mesh of Object.values(w.__resourceObjects) as any[]) {
      mesh.geometry.dispose();
      mesh.material.dispose();
    }
    return { samples, before, after: { ...w.__resourceStats } };
  });
  const initial = evidence.samples[0];
  for (const sample of evidence.samples) {
    expect(new Set(sample.geometryIds).size).toBe(3);
    expect(new Set(sample.objectIds).size).toBe(3);
    expect(new Set(sample.materialIds).size).toBe(3);
    expect(sample.disposalListeners).toEqual([1, 1, 1]);
    expect(sample.created - sample.deleted).toBe(
      initial.created - initial.deleted,
    );
  }
  // Each current plane owns position/normal/uv and index buffers. All three
  // graphs must release them immediately; no GC or context teardown is required.
  expect(evidence.after.deleted - evidence.before.deleted).toBe(12);
  await test.info().attach("resource-lifecycle", {
    body: JSON.stringify(evidence, null, 2),
    contentType: "application/json",
  });
});

for (const source_color_space of [
  "Linear Rec.709 (sRGB)",
  "Linear Rec.2020",
  "ACEScg",
  "ACES2065-1",
] as MotionSourceSpace[]) {
  test(`native linear source ${source_color_space}`, async ({ page }) => {
    const spec: MotionSpec = {
      engine: "three-float-v1",
      source_color_space,
      name: "native",
      code,
      width: 16,
      height: 16,
      frames: 2,
      fps: { numerator: 30000, denominator: 1001 },
    };
    const frame = await mount(page, spec);
    const actual = await capture(frame);
    expect(actual.probe.source_space).toBe(source_color_space);
    [-0.125, 0.06172835, 2.0625, 0.5].forEach((value, index) =>
      expect(actual.pixel[index]).toBeCloseTo(value, 6),
    );
    const colors = await frame.evaluate(() => (window as any).__authorColors);
    [0.2, 0.3, 0.4].forEach((value, index) =>
      expect(colors.numeric[index]).toBeCloseTo(value, 6),
    );
    const call = await api(page);
    const expected = await call<{ colors: number[][] }>("convert_colors", {
      colors: [[0, 1, 0]],
      // #00ff00 decodes analytically to linear (0,1,0). Compare primaries without
      // introducing OCIO's approximate sRGB CPU power curve into this assertion.
      source: "Linear Rec.709 (sRGB)",
      output: source_color_space,
    });
    expected.colors[0].forEach((value, index) =>
      expect(colors.css[index]).toBeCloseTo(value, 5),
    );
    const display = await frame.evaluate(() =>
      (window as any).__chromaFloat.sampleDisplay(0, 0, 0),
    );
    const displayReference = await call<{ colors: number[][] }>(
      "convert_colors",
      {
        colors: [[-0.25, 0.1234567, 4.125]],
        source: source_color_space,
        output: "sRGB",
      },
    );
    // The displayed GPU path must match the corresponding CPU view, with byte
    // quantization tolerance. This checks AP0/AP1 white adaptation in the preview.
    displayReference.colors[0].forEach((value, index) => {
      expect(
        Math.abs(
          display[index] - Math.round(Math.min(1, Math.max(0, value)) * 255),
        ),
      ).toBeLessThanOrEqual(2);
    });
    const basic = {
      ...spec,
      code: `import * as THREE from 'three';
      const color=new THREE.Color(0.2,0.3,0.4);
      export default function createScene(){const scene=new THREE.Scene(),camera=new THREE.Camera();
      scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),new THREE.MeshBasicMaterial({color,toneMapped:false})));
      return {scene,camera,update(){}};}`,
    };
    const basicPixel = (await capture(await mount(page, basic))).pixel;
    [0.2, 0.3, 0.4, 1].forEach((value, index) =>
      expect(basicPixel[index]).toBeCloseTo(value, 5),
    );
  });
}

test("upgrade preserves pixels, alpha and new native foreground", async ({
  page,
}) => {
  const call = await api(page);
  const { project } = await call<{ project: { id: string } }>(
    "create_project",
    { name: "linear-upgrade" },
  );
  const initial = await call<MotionReply>("create_motion_scene", {
    project_id: project.id,
    spec: {
      engine: "three-float-v1",
      name: "old",
      code,
      width: 16,
      height: 16,
      frames: 2,
      fps: { numerator: 30, denominator: 1 },
    },
  });
  const original = await capture(await mount(page, initial.scene));
  let saved = initial;
  for (const source_color_space of [
    "Linear Rec.2020",
    "ACEScg",
    "ACES2065-1",
  ] as MotionSourceSpace[]) {
    saved = await call<MotionReply>("upgrade_motion_scene", {
      scene_id: saved.record.id,
      expected_revision_id: saved.revision_id,
      source_color_space,
    });
    const actual = await capture(await mount(page, saved.scene));
    const restored = await call<{ colors: number[][] }>("convert_colors", {
      colors: [actual.pixel.slice(0, 3)],
      source: source_color_space,
      output: "Linear Rec.709 (sRGB)",
    });
    restored.colors[0].forEach((value, index) =>
      expect(value).toBeCloseTo(original.pixel[index], 5),
    );
    expect(actual.pixel[3]).toBe(original.pixel[3]);
  }
  // A native opaque foreground can author AP0 directly after upgrade, while the
  // old layer remains intact below it. This is a color upgrade, not recoloring.
  const foreground = {
    ...saved.scene,
    code: code
      .replace("transparent:true", "transparent:false")
      .replace("vec4(-0.25,0.1234567,4.125,0.5)", "vec4(0.,1.,0.,1.)"),
  };
  const pixel = (await capture(await mount(page, foreground))).pixel;
  [0, 1, 0, 1].forEach((value, index) =>
    expect(pixel[index]).toBeCloseTo(value, 6),
  );
  const translucent = {
    ...saved.scene,
    code: code.replace(
      "vec4(-0.25,0.1234567,4.125,0.5)",
      "vec4(0.,1.,0.,0.25)",
    ),
  };
  const mixed = (await capture(await mount(page, translucent))).pixel;
  const background = await call<{ colors: number[][] }>("convert_colors", {
    colors: [original.pixel.slice(0, 3)],
    source: "Linear Rec.709 (sRGB)",
    output: "ACES2065-1",
  });
  background.colors[0].forEach((value, index) =>
    expect(mixed[index]).toBeCloseTo(
      value * 0.75 + (index === 1 ? 0.25 : 0),
      5,
    ),
  );
  expect(mixed[3]).toBeCloseTo(0.625, 6);
});

test("native and retained backgrounds can return to transparent coverage", async ({
  page,
}) => {
  const background = `import * as THREE from 'three';
    export default function createScene(){const scene=new THREE.Scene(),camera=new THREE.Camera();
    return {scene,camera,update({frame}){scene.background=frame===0?new THREE.Color(.2,.3,.4):null;}};}`;
  const blank = `import * as THREE from 'three';export default function createScene(){return {scene:new THREE.Scene(),camera:new THREE.Camera(),update(){}};}`;
  for (const retained of [false, true]) {
    const spec: MotionSpec = {
      engine: "three-float-v1",
      source_color_space: "ACEScg",
      name: "background",
      code: retained ? blank : background,
      width: 16,
      height: 16,
      frames: 2,
      fps: { numerator: 30, denominator: 1 },
      preserved_layers: retained
        ? [{ code: background, source_color_space: "Linear Rec.709 (sRGB)" }]
        : [],
    };
    const frame = await mount(page, spec);
    expect((await capture(frame, 0)).pixel[3]).toBe(1);
    expect((await capture(frame, 1)).pixel).toEqual([0, 0, 0, 0]);
  }
});

test("linear pipeline UI preserves drafts and supports keyboard and accessibility", async ({
  page,
}) => {
  const call = await api(page);
  const { project } = await call<{ project: { id: string } }>(
    "create_project",
    { name: "宽色域界面" },
  );
  const saved = await call<MotionReply>("create_motion_scene", {
    project_id: project.id,
    spec: {
      engine: "three-float-v1",
      name: "709 原稿",
      code,
      width: 128,
      height: 72,
      frames: 30,
      fps: { numerator: 30, denominator: 1 },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区", exact: true }).click();
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page
    .getByLabel("当前作品", { exact: true })
    .selectOption(`motion:${saved.record.id}`);
  const target = page.getByLabel("管线目标空间", { exact: true });
  await target.selectOption("ACES2065-1");
  await page.getByRole("button", { name: "升级色彩管线", exact: true }).click();
  await expect(page.getByLabel("编辑图层", { exact: true })).toBeVisible();
  await expect(
    page.getByText("当前创作空间：ACES2065-1", { exact: false }),
  ).toBeVisible();
  await page.getByLabel("编辑图层", { exact: true }).selectOption("0");
  await expect(
    page.getByRole("textbox", { name: "动画 TypeScript 代码", exact: true }),
  ).toContainText("__authorColors");
  await page.getByLabel("动画名称", { exact: true }).fill("未保存的宽色域草稿");
  await target.selectOption("ACEScg");
  await expect(
    page.getByRole("button", { name: "升级色彩管线", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "保存作品", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "升级色彩管线", exact: true }),
  ).toBeEnabled();
  await target.focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Tab");
  await expect(page.locator("iframe[title='动画画面']")).toBeVisible({
    timeout: 20000,
  });
  const audit = await new AxeBuilder({ page }).analyze();
  expect(audit.violations).toEqual([]);
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/motion-linear-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(target).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "../reports/motion-linear-mobile.png",
    fullPage: true,
  });
  // Delay only the mutation response, after the server has accepted the revision.
  // A newer local metadata draft must survive this uncertain-completion window.
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let accepted = false;
  await page.route("**/api/operations/upgrade_motion_scene", async (route) => {
    const response = await route.fetch();
    accepted = true;
    await gate;
    await route.fulfill({ response });
  });
  await target.selectOption("Linear Rec.2020");
  await page.getByRole("button", { name: "升级色彩管线", exact: true }).click();
  await expect.poll(() => accepted).toBeTruthy();
  await page.getByLabel("动画名称", { exact: true }).fill("升级期间的本地修改");
  release();
  await expect(
    page.getByText(
      "检测到外部更新，已保留未保存修改。可复制代码，或载入最新版本。",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByLabel("动画名称", { exact: true })).toHaveValue(
    "升级期间的本地修改",
  );
});
