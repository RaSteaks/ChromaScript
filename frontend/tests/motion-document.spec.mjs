import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { compileFloatDocument, loadFloatDocument } from "../motion/runtime.mjs";
import { transportVideoFrames } from "../motion/video-transport.mjs";

// Real OCIO tables reproduce the oversized PQ export document without padding,
// personal project data, or launching the production Chromium driver.
const request = JSON.parse(
  execFileSync(
    "./.venv/bin/python",
    [
      "-c",
      `
import json
from chromascript.motion import MotionSpec
from chromascript.motion_color import video_shader
spec = MotionSpec(source_color_space="ACEScg", width=32, height=18, frames=1, code='''
import * as THREE from "three";
export default function createScene() {
  const scene = new THREE.Scene(), camera = new THREE.Camera();
  scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), new THREE.ShaderMaterial({
    vertexShader: "void main(){gl_Position=vec4(position.xy,0.,1.);}",
    fragmentShader: "void main(){gl_FragColor=vec4(.25,.5,2.,1.);}"
  })));
  return {scene, camera, update(){}};
}
''')
print(json.dumps({"spec":spec.model_dump(), "display":spec.display(),
                 "video_capture":video_shader("ACEScg", "PQ", "p210le")}))
`,
    ],
    { cwd: "..", encoding: "utf8", maxBuffer: 8 * 1024 ** 2 },
  ),
);

for (const mode of ["frames", "video"]) {
  test(`${mode} document loads with an opaque origin and captures its first frame`, async ({
    page,
  }, testInfo) => {
    const { html } = await compileFloatDocument(
      {
        ...request,
        video_capture: mode === "video" ? request.video_capture : undefined,
      },
      mode,
    );
    if (mode === "video")
      expect(
        Buffer.byteLength(Buffer.from(html).toString("base64")),
      ).toBeGreaterThan(2 * 1024 ** 2);
    const requests = [];
    await page.route("**/*", (route) => {
      requests.push(route.request().url());
      return route.abort();
    });
    await loadFloatDocument(
      {
        goto: ({ url, timeout }) => {
          // WebKit accepts longer URLs: enforce the production navigation budget
          // here so restoring Base64 navigation fails this regression on WebKit too.
          expect(Buffer.byteLength(url)).toBeLessThan(1024);
          return page.goto(url, { timeout });
        },
        evaluate: (...args) => page.evaluate(...args),
      },
      html,
    );
    expect(await page.evaluate(() => window.__chromaError)).toBeUndefined();
    expect(await page.evaluate(() => window.origin)).toBe("null");
    expect(
      await page.evaluate(async () => {
        try {
          await fetch("https://example.invalid/blocked");
          return false;
        } catch {
          return true;
        }
      }),
    ).toBe(true);
    expect(requests).toEqual([]);
    if (mode === "frames") {
      const pixel = await page.evaluate(() => {
        window.__chromaFloat.capture(0);
        const bytes = Uint8Array.from(
          atob(window.__chromaFloat.readChunk(0, 16)),
          (c) => c.charCodeAt(0),
        );
        return Array.from(new Float32Array(bytes.buffer));
      });
      expect(pixel).toEqual([0.25, 0.5, 2, 1]);
    } else {
      await transportVideoFrames(
        page,
        [0],
        true,
        async (frame, pixels, thumbnail) => {
          expect(frame).toBe(0);
          expect(pixels.length).toBe(32 * 18 * 4); // P210: two 16-bit samples per pixel.
          expect(thumbnail.length).toBe(32 * 18 * 4);
        },
      );
    }
    await page.screenshot({
      path: testInfo.outputPath(`${mode}-document.png`),
    });
    await testInfo.attach("document-size", {
      body: JSON.stringify({
        htmlBytes: Buffer.byteLength(html),
        navigationURL: page.url(),
      }),
      contentType: "application/json",
    });
  });
}

test("document loading preserves scene initialization errors", async ({
  page,
}) => {
  const { html } = await compileFloatDocument(
    {
      ...request,
      spec: {
        ...request.spec,
        code: 'export default function(){throw new Error("scene initialization failed");}',
      },
    },
    "frames",
  );
  await loadFloatDocument(
    {
      goto: ({ url, timeout }) => page.goto(url, { timeout }),
      evaluate: (...args) => page.evaluate(...args),
    },
    html,
  );
  expect(await page.evaluate(() => window.__chromaError)).toBe(
    "scene initialization failed",
  );
});
