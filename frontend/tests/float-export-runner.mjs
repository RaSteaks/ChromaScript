// Test-only driver: exercise the final-raster document and real Node/Python ACK
// protocol in isolated, headless WebKit. Never invoke the production Chrome driver.
import { webkit } from "@playwright/test";
import { createInterface } from "node:readline";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import {
  transportVideoFrames,
  writeVideoPacket,
} from "../motion/video-transport.mjs";

// Benchmark-only overrides load a frozen compiler/transport from a temporary
// directory. Production never reads these environment variables.
const { compileFloatDocument, loadFloatDocument } = await import(
  process.env.CHROMASCRIPT_TEST_COMPILER
    ? pathToFileURL(process.env.CHROMASCRIPT_TEST_COMPILER).href
    : "../motion/runtime.mjs"
);
const { transportFloatFrames } = await import(
  process.env.CHROMASCRIPT_TEST_TRANSPORT
    ? pathToFileURL(process.env.CHROMASCRIPT_TEST_TRANSPORT).href
    : "../motion/float-transport.mjs"
);

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
const lines = input[Symbol.asyncIterator]();
let browser;
try {
  const first = await lines.next();
  const request = JSON.parse(first.value);
  const { html } = await compileFloatDocument(request, "frames");
  browser = await webkit.launch({ headless: true });
  const shutdown = async () => {
    await browser.close();
    process.exit(1);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
  const page = await browser.newPage();
  await page.route("**/*", (route) => route.abort());
  // Exercise the production loader, including its opaque origin, without Chrome.
  // Older frozen benchmark compilers predate the shared document loader.
  if (loadFloatDocument) {
    await loadFloatDocument(
      {
        goto: ({ url, timeout }) => page.goto(url, { timeout }),
        evaluate: (...args) => page.evaluate(...args),
      },
      html,
    );
  } else {
    await page.setContent('<iframe sandbox="allow-scripts"></iframe>');
    await page.locator("iframe").evaluate((element, document) => {
      element.srcdoc = document;
    }, html);
  }
  const frame = loadFloatDocument
    ? page.mainFrame()
    : page.frames().find((item) => item.parentFrame());
  await frame.waitForFunction(
    () => window.__chromaFloat || window.__chromaError,
  );
  const state = await frame.evaluate(() => ({
    error: window.__chromaError,
    probe: window.__chromaFloat?.probe,
  }));
  if (state.error) throw new Error(state.error);
  mkdirSync(request.frames, { recursive: true });
  writeFileSync(join(request.frames, "gpu.json"), JSON.stringify(state.probe));
  const accept = async (index) => {
    const reply = await lines.next();
    if (reply.done || JSON.parse(reply.value).accepted !== index)
      throw new Error("Invalid ACK");
  };
  const timing =
    process.argv[2] === "video"
      ? await transportVideoFrames(
          frame,
          request.frame_indices,
          request.thumbnail,
          async (index, pixels, thumbnail) => {
            await writeVideoPacket(index, pixels, thumbnail);
            await accept(index);
          },
        )
      : await transportFloatFrames(
          frame,
          request.frame_indices,
          request.frames,
          async (index) => {
            process.stdout.write(JSON.stringify({ frame: index }) + "\n");
            await accept(index);
          },
        );
  writeFileSync(join(request.frames, "transport.json"), JSON.stringify(timing));
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  input.close();
  process.stdin.destroy();
  await browser?.close();
}
