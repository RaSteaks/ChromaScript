import { test, expect, type Page } from "@playwright/test";
import type { MotionSpec } from "../src/types";

// Only compile source and execute the shared renderer in headless WebKit.
// Production render operations launch Chrome and must never be used by these tests.
async function mount(
  page: Page,
  body: string,
  options: Partial<MotionSpec> = {},
) {
  const { token } = await (await page.request.get("/api/session")).json();
  const spec: MotionSpec = {
    engine: "three-float-v1",
    source_color_space: "ACEScg",
    name: "HDR contract regression",
    width: 32,
    height: 32,
    frames: 3,
    fps: { numerator: 30000, denominator: 1001 },
    code: source(body),
    ...options,
  };
  const response = await page.request.post(
    "/api/operations/compile_motion_preview",
    {
      headers: { Authorization: `Bearer ${token}` },
      data: { spec, hdr_preview: true },
    },
  );
  expect(response.ok(), await response.text()).toBeTruthy();
  const { html } = await response.json();
  await page.goto("/");
  await page.evaluate((html) => {
    document.body.innerHTML =
      '<iframe sandbox="allow-scripts" style="width:320px;height:320px"></iframe>';
    document.querySelector("iframe")!.srcdoc = html;
  }, html);
  const frame = page.frames().find((item) => item.parentFrame())!;
  await frame.waitForFunction(
    () => !!(window as any).__chromaFloat || !!(window as any).__chromaError,
  );
  return frame;
}

function source(body: string) {
  return `import * as THREE from 'three';
  export default function createScene(){
    const scene=new THREE.Scene();
    let camera=new THREE.Camera();
    let update=()=>{};
    ${body}
    return {scene,camera,update};
  }`;
}

const texture = `const tex=new THREE.DataTexture(
  new Float32Array([-.25,.1234567,4.125,.5]),1,1,THREE.RGBAFormat,THREE.FloatType);
  tex.needsUpdate=true;`;
const plane = (material: string) =>
  `scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),${material}));`;
const shader = (options = "") => `new THREE.ShaderMaterial({
  ${options} depthTest:false,toneMapped:false,
  vertexShader:'void main(){gl_Position=vec4(position.xy,0.,1.);}',
  fragmentShader:'void main(){gl_FragColor=vec4(2.,2.,2.,.5);}'})`;

async function capture(frame: Awaited<ReturnType<typeof mount>>, index = 0) {
  expect(
    await frame.evaluate(() => (window as any).__chromaError),
  ).toBeUndefined();
  return frame.evaluate((index) => {
    const bridge = (window as any).__chromaFloat;
    const result = bridge.capture(index);
    const raw = bridge.readChunk(0, result.bytes);
    const bytes = Uint8Array.from(atob(raw), (c) => c.charCodeAt(0));
    const pixel = Array.from(new Float32Array(bytes.buffer).slice(0, 4));
    const hdr = new Uint16Array(bridge.captureHDR(index));
    return {
      pixel,
      raw,
      p010: [hdr[0] >> 6, hdr[1024] >> 6, hdr[1025] >> 6],
      invalidP010: Array.from(hdr).some((word) => word & 63),
    };
  }, index);
}

function expectPixel(actual: number[], expected: number[]) {
  expected.forEach((value, index) =>
    expect(actual[index]).toBeCloseTo(value, 6),
  );
}

for (const kind of ["flat", "cube"] as const) {
  test(`${kind} HDR background associates RGB once before master and PQ capture`, async ({
    page,
  }, info) => {
    const background =
      kind === "flat"
        ? "tex"
        : `Object.assign(new THREE.CubeTexture(Array(6).fill(tex)),{
          type:THREE.FloatType,needsUpdate:true})`;
    const body = `${texture}
      ${kind === "cube" ? "camera=new THREE.PerspectiveCamera(90,1,.1,10);" : ""}
      const background=${background};
      scene.background=background;
      update=({frame})=>{scene.background=frame===1?null:background;};`;
    const frame = await mount(page, body);
    const actual = await capture(frame);
    expectPixel(actual.pixel, [-0.125, 0.06172835, 2.0625, 0.5]);
    expect(actual.invalidP010).toBe(false);
    expectPixel((await capture(frame, 1)).pixel, [0, 0, 0, 0]);
    expectPixel((await capture(frame, 2)).pixel, actual.pixel);
    const screenshot = await page.screenshot();
    // A transparent material is an independent, already-tested association path.
    const reference = await capture(
      await mount(
        page,
        `${texture}${plane("new THREE.MeshBasicMaterial({map:tex,transparent:true,toneMapped:false})")}`,
      ),
    );
    expectPixel(actual.pixel, reference.pixel);
    expect(actual.p010).toEqual(reference.p010);
    await info.attach(`${kind}-rgba32f`, {
      body: Buffer.from(actual.raw, "base64"),
      contentType: "application/octet-stream",
    });
    await info.attach(`${kind}-pixel-and-pq`, {
      body: JSON.stringify({ pixel: actual.pixel, p010: actual.p010 }),
      contentType: "application/json",
    });
    await info.attach(`${kind}-preview`, {
      body: screenshot,
      contentType: "image/png",
    });
  });
}

test("background blending keeps opaque foreground ahead and combines retained layers", async ({
  page,
}) => {
  const background = `${texture}scene.background=tex;`;
  const retained = [
    {
      source_color_space: "ACEScg" as const,
      code: source("scene.background=new THREE.Color(1,2,3);"),
    },
  ];
  const mixed = await capture(
    await mount(page, background, {
      preserved_layers: retained,
    }),
  );
  expectPixel(mixed.pixel, [0.375, 1.06172835, 3.5625, 1]);
  // If the background accidentally enters Three's transparent draw queue, it
  // renders after this opaque plane and changes the foreground's pixels.
  const foreground = await capture(
    await mount(
      page,
      `${background}${plane("new THREE.MeshBasicMaterial({color:new THREE.Color(4,2,-.25),toneMapped:false})")}`,
      { preserved_layers: retained },
    ),
  );
  expectPixel(foreground.pixel, [4, 2, -0.25, 1]);
  const layer = await capture(
    await mount(page, "", {
      preserved_layers: [
        { code: source(background), source_color_space: "ACEScg" },
      ],
    }),
  );
  expectPixel(layer.pixel, [-0.125, 0.06172835, 2.0625, 0.5]);
});

test("override shader coverage tracks transparency changes and allowOverride", async ({
  page,
}) => {
  const frame = await mount(
    page,
    `
    const material=${shader()};
    const original=new THREE.MeshBasicMaterial({color:new THREE.Color(3,1,2)});
    ${plane("original")}
    scene.overrideMaterial=material;
    update=({frame})=>{
      material.transparent=frame===1;
      original.allowOverride=frame!==2;
    };`,
  );
  expectPixel((await capture(frame, 0)).pixel, [2, 2, 2, 1]);
  expectPixel((await capture(frame, 1)).pixel, [1, 1, 1, 0.5]);
  expectPixel((await capture(frame, 2)).pixel, [3, 1, 2, 1]);
  expectPixel((await capture(frame, 0)).pixel, [2, 2, 2, 1]);
});

test("override materials enforce the same blending and opacity rules", async ({
  page,
}) => {
  for (const options of [
    "transparent:true,blending:THREE.NoBlending,",
    "opacity:.5,",
  ]) {
    const frame = await mount(
      page,
      `
      ${plane("new THREE.MeshBasicMaterial()")}
      scene.overrideMaterial=${shader(options)};`,
    );
    expect(await frame.evaluate(() => (window as any).__chromaError)).toMatch(
      /NormalBlending|transparent/,
    );
  }
  // A material excluded from override must not cause an unused invalid override
  // to reject an otherwise valid scene.
  const excluded = await capture(
    await mount(
      page,
      `
    ${plane("new THREE.MeshBasicMaterial({allowOverride:false,color:new THREE.Color(2,1,3)})")}
    scene.overrideMaterial=${shader("opacity:.5,")};`,
    ),
  );
  expectPixel(excluded.pixel, [2, 1, 3, 1]);
});

for (const container of [
  "direct",
  "array",
  "struct",
  "struct-array",
] as const) {
  test(`foreign texture in ${container} uniform is rejected, including override`, async ({
    page,
  }) => {
    const value = {
      direct: "tex",
      array: "[tex]",
      struct: "{map:tex}",
      "struct-array": "[{map:tex}]",
    }[container];
    for (const override of [false, true]) {
      const frame = await mount(
        page,
        `${texture}
        tex.colorSpace=THREE.SRGBColorSpace;
        const material=${shader()};
        material.uniforms.hidden={value:${value}};
        ${plane(override ? "new THREE.MeshBasicMaterial()" : "material")}
        ${override ? "scene.overrideMaterial=material;" : ""}`,
      );
      expect(
        await frame.evaluate(() => (window as any).__chromaError),
      ).toContain("宽色域颜色纹理");
    }
  });
}

test("native sampler structures and cyclic unused uniform containers remain valid", async ({
  page,
}) => {
  const frame = await mount(
    page,
    `${texture}
    const material=new THREE.ShaderMaterial({
      transparent:true,depthTest:false,uniforms:{items:{value:[{map:tex}]}},
      vertexShader:'void main(){gl_Position=vec4(position.xy,0.,1.);}',
      fragmentShader:'struct Item{sampler2D map;};uniform Item items[1];void main(){gl_FragColor=texture2D(items[0].map,vec2(.5));}'});
    // The same texture is reachable through aliases and a cyclic unused struct;
    // numeric typed arrays must not be walked one scalar at a time.
    const cycle={map:tex,values:new Float32Array(16)};
    cycle.self=cycle;
    material.uniforms.extra={value:cycle};
    ${plane("material")}`,
  );
  expectPixel((await capture(frame)).pixel, [-0.125, 0.06172835, 2.0625, 0.5]);
});

test("nested texture color tags are rechecked after each source update", async ({
  page,
}) => {
  const frame = await mount(
    page,
    `${texture}
    const material=${shader()};
    material.uniforms.nested={value:{maps:[tex]}};
    ${plane("material")}
    update=({frame})=>{
      tex.colorSpace=frame===1?THREE.SRGBColorSpace:THREE.NoColorSpace;
    };`,
  );
  expectPixel((await capture(frame, 0)).pixel, [2, 2, 2, 1]);
  // A per-frame visited set must not turn into a permanent approval of mutable
  // author resources. Returning to a valid frame should remain possible.
  await expect(capture(frame, 1)).rejects.toThrow("宽色域颜色纹理");
  expectPixel((await capture(frame, 2)).pixel, [2, 2, 2, 1]);
});
