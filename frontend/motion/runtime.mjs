// Source is compiled as browser code only. Never import/evaluate it in Node.
import { build, formatMessages } from "esbuild";
import ts from "typescript";
import { randomUUID } from "node:crypto";
import {
  readFileSync,
  writeFileSync,
  mkdirSync,
  openSync,
  writeSync,
  closeSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const allowed = new Set(["three"]);
const policy =
  "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; media-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'";

function validateImports(code) {
  const source = ts.createSourceFile(
    "Animation.ts",
    code,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  function check(specifier) {
    if (
      !specifier ||
      !ts.isStringLiteral(specifier) ||
      !allowed.has(specifier.text)
    ) {
      throw new Error(
        `仅允许导入：${[...allowed].join(", ")}。动画在本地离线运行，不支持文件路径或网络模块。`,
      );
    }
  }
  function visit(node) {
    if (
      ts.isImportDeclaration(node) ||
      (ts.isExportDeclaration(node) && node.moduleSpecifier)
    )
      check(node.moduleSpecifier);
    if (ts.isCallExpression(node)) {
      if (node.expression.kind === ts.SyntaxKind.ImportKeyword)
        check(node.arguments[0]);
      if (
        ts.isIdentifier(node.expression) &&
        node.expression.text === "require"
      )
        throw new Error("请使用静态 import；动画不允许 require()。");
    }
    // TypeScript import-equals can otherwise bypass ordinary ES import validation.
    if (ts.isImportEqualsDeclaration(node))
      throw new Error("请使用标准 ES import。");
    ts.forEachChild(node, visit);
  }
  visit(source);
}

async function compile(spec, mode, channel, display, maxDimension) {
  validateImports(spec.code);
  const config = {
    width: spec.width,
    height: spec.height,
    frames: spec.frames,
    fps: spec.fps.numerator / spec.fps.denominator,
    channel,
    frameRate: spec.fps,
    preview: mode === "preview",
    display,
    maxDimension,
  };
  const entry = readFileSync(join(root, "motion/float-runtime.mjs"), "utf8");
  return build({
    stdin: {
      contents: entry,
      sourcefile: "motion-entry.ts",
      loader: "ts",
      resolveDir: root,
    },
    // Every mode uses the identical isolated float renderer and local packages.
    absWorkingDir: root,
    write: false,
    bundle: true,
    minify: true,
    platform: "browser",
    format: "iife",
    jsx: "automatic",
    target: "es2022",
    define: { "process.env.NODE_ENV": '"production"' },
    logLevel: "silent",
    plugins: [
      {
        name: "local-animation",
        setup(plugin) {
          plugin.onResolve({ filter: /^chromascript:scene$/ }, () => ({
            path: "Animation.ts",
            namespace: "scene",
          }));
          plugin.onLoad({ filter: /.*/, namespace: "scene" }, () => ({
            contents: spec.code,
            loader: "ts",
            resolveDir: root,
          }));
          plugin.onResolve({ filter: /^chromascript:config$/ }, () => ({
            path: "config",
            namespace: "config",
          }));
          plugin.onLoad({ filter: /.*/, namespace: "config" }, () => ({
            contents: JSON.stringify(config),
            loader: "json",
          }));
          plugin.onResolve({ filter: /.*/, namespace: "scene" }, (args) => {
            if (!allowed.has(args.path))
              return { errors: [{ text: `不允许导入 ${args.path}。` }] };
            return plugin.resolve(args.path, {
              resolveDir: root,
              kind: args.kind,
            });
          });
        },
      },
    ],
  });
}

async function main() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const request = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  const spec = request.spec || request;
  const channel = randomUUID();
  const result = await compile(
    spec,
    process.argv[2],
    channel,
    request.display,
    request.max_dimension,
  );
  const script = result.outputFiles[0].text.replace(
    /<\/script/gi,
    "<\\/script",
  );
  // Match the host color scheme so Chromium does not force an opaque white iframe
  // backdrop behind a correctly transparent float-scene canvas.
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}"><style>html,body,#root{color-scheme:dark;margin:0;width:100%;height:100%;overflow:hidden;background:transparent}</style></head><body><div id="root"></div><script>${script}</script></body></html>`;
  if (process.argv[2] === "preview") {
    process.stdout.write(JSON.stringify({ html, channel }));
    return;
  }
  if (process.argv[2] !== "frames")
    throw new Error("Unknown animation runtime mode");
  // Reuse the installed, pinned Chromium driver, but never screenshot its DOM.
  // Source executes only in a network-disabled opaque data document, not Node.
  const { openBrowser } = await import("@remotion/renderer");
  const browser = await openBrowser("chrome", {
    browserExecutable: request.browser,
    chromiumOptions: { gl: "angle" },
    logLevel: "error",
  });
  const shutdown = async () => {
    await browser.close({ silent: true });
    process.exit(1);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
  try {
    const page = await browser.newPage({
      context: () => null,
      logLevel: "error",
      indent: false,
      pageIndex: 0,
      onBrowserLog: null,
      onLog: () => {},
    });
    await page._client().send("Network.enable");
    await page._client().send("Network.setBlockedURLs", {
      urls: ["http://*", "https://*", "file://*"],
    });
    await page.goto({
      url: "data:text/html;base64," + Buffer.from(html).toString("base64"),
      timeout: 30000,
    });
    const state = await page.evaluate(() => ({
      error: window.__chromaError,
      probe: window.__chromaFloat?.probe,
    }));
    if (state.error || !state.probe)
      throw new Error(state.error || "Float renderer did not initialize");
    mkdirSync(request.frames, { recursive: true });
    writeFileSync(
      join(request.frames, "gpu.json"),
      JSON.stringify(state.probe),
    );
    // Bounded binary chunks avoid JSON-number arrays and whole-sequence RAM use.
    for (const frame of request.frame_indices ||
      Array.from({ length: spec.frames }, (_, i) => i)) {
      const capture = await page.evaluate(
        (f) => window.__chromaFloat.capture(f),
        frame,
      );
      if (capture.bytes !== capture.width * capture.height * 16)
        throw new Error("Float frame dimensions mismatch");
      const path = join(request.frames, `frame-${frame}.f32`);
      const fd = openSync(path, "w");
      try {
        for (let offset = 0; offset < capture.bytes; offset += 1048576) {
          const data = await page.evaluate(
            (o) => window.__chromaFloat.readChunk(o, 1048576),
            offset,
          );
          writeSync(fd, Buffer.from(data, "base64"));
        }
      } finally {
        closeSync(fd);
      }
      process.stdout.write(
        JSON.stringify({ completed: frame + 1, frame }) + "\n",
      );
    }
  } finally {
    await browser.close({ silent: true });
  }
}
main().catch(async (error) => {
  if (error.errors) {
    const messages = await formatMessages(error.errors, {
      kind: "error",
      color: false,
    });
    process.stderr.write(messages.join("\n"));
  } else process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
});
