// Source is compiled as browser code only. Never import/evaluate it in Node.
import { build, formatMessages } from "esbuild";
import ts from "typescript";
import { randomUUID } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const allowed = new Set([
  "react",
  "react/jsx-runtime",
  "remotion",
  "@remotion/three",
  "@react-three/fiber",
  "three",
]);
const policy =
  "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; media-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'";

function validateImports(code) {
  const source = ts.createSourceFile(
    "Animation.tsx",
    code,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
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

async function compile(spec, mode, channel) {
  validateImports(spec.code);
  const config = {
    width: spec.width,
    height: spec.height,
    frames: spec.frames,
    fps: spec.fps.numerator / spec.fps.denominator,
    channel,
  };
  const entry =
    mode === "preview"
      ? readFileSync(join(root, "motion/preview.tsx"), "utf8")
      : `import React from 'react';
       import {Composition, registerRoot} from 'remotion';
       import Scene from 'chromascript:scene';
       registerRoot(() => <Composition id="Animation" component={Scene} width={${spec.width}} height={${spec.height}} fps={${config.fps}} durationInFrames={${spec.frames}} />);`;
  return build({
    stdin: {
      contents: entry,
      sourcefile: "motion-entry.tsx",
      loader: "tsx",
      resolveDir: root,
    },
    // Export keeps package imports external so Webpack shares exactly one React
    // and Remotion registry with its rendering shell. Preview is self-contained.
    absWorkingDir: root,
    write: false,
    bundle: true,
    minify: true,
    platform: "browser",
    format: mode === "preview" ? "iife" : "esm",
    packages: mode === "preview" ? undefined : "external",
    jsx: "automatic",
    target: "es2022",
    define: { "process.env.NODE_ENV": '"production"' },
    logLevel: "silent",
    plugins: [
      {
        name: "local-animation",
        setup(plugin) {
          plugin.onResolve({ filter: /^chromascript:scene$/ }, () => ({
            path: "Animation.tsx",
            namespace: "scene",
          }));
          plugin.onLoad({ filter: /.*/, namespace: "scene" }, () => ({
            contents: spec.code,
            loader: "tsx",
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
            if (mode !== "preview") return { path: args.path, external: true };
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
  const result = await compile(spec, process.argv[2], channel);
  const javascript = result.outputFiles[0].text;
  if (process.argv[2] === "preview") {
    // Opaque iframe origin plus CSP keep source away from editor cookies/tokens
    // and network requests. Escaping script terminators prevents HTML injection.
    const script = javascript.replace(/<\/script/gi, "<\\/script");
    const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}"><style>html,body,#root{margin:0;width:100%;height:100%;overflow:hidden;background:transparent}*{box-sizing:border-box}</style></head><body><div id="root"></div><script>${script}</script></body></html>`;
    process.stdout.write(JSON.stringify({ html, channel }));
    return;
  }
  if (process.argv[2] !== "frames")
    throw new Error("Unknown animation runtime mode");
  const { bundle } = await import("@remotion/bundler");
  const { renderFrames } = await import("@remotion/renderer");
  mkdirSync(request.directory, { recursive: true });
  const entryPoint = join(request.directory, "compiled.js");
  writeFileSync(entryPoint, javascript);
  const serveUrl = await bundle({
    entryPoint,
    outDir: join(request.directory, "bundle"),
    rootDir: root,
    publicDir: null,
    enableCaching: false,
    ignoreRegisterRootWarning: true,
    webpackOverride: (config) => ({
      ...config,
      // External source maps keep Remotion's error reporting functional without
      // Webpack eval(), which would require weakening the document CSP.
      devtool: "source-map",
      resolve: {
        ...config.resolve,
        modules: [
          join(root, "node_modules"),
          ...(config.resolve?.modules || []),
        ],
      },
    }),
  });
  // A fixed HTTP document policy is used before any composition JavaScript runs.
  const index = join(serveUrl, "index.html");
  writeFileSync(
    index,
    readFileSync(index, "utf8").replace(
      /<head>/i,
      `<head><meta http-equiv="Content-Security-Policy" content="${policy}"><link rel="icon" href="data:,">`,
    ),
  );
  await renderFrames({
    serveUrl,
    browserExecutable: request.browser,
    composition: {
      id: "Animation",
      width: spec.width,
      height: spec.height,
      fps: spec.fps.numerator / spec.fps.denominator,
      durationInFrames: spec.frames,
      defaultProps: {},
      props: {},
    },
    outputDir: request.frames,
    imageFormat: "png",
    imageSequencePattern: "frame-[frame].png",
    concurrency: 1,
    muted: true,
    timeoutInMilliseconds: 15000,
    chromiumOptions: { gl: "angle" },
    logLevel: "error",
    onStart: () =>
      process.stdout.write(
        JSON.stringify({ stage: "rendering", completed: 0 }) + "\n",
      ),
    onFrameUpdate: (completed) =>
      process.stdout.write(
        JSON.stringify({ stage: "rendering", completed }) + "\n",
      ),
  });
}

main().catch(async (error) => {
  const message = error.errors
    ? (
        await formatMessages(error.errors, { kind: "error", color: false })
      ).join("\n")
    : String(error.message || error);
  process.stderr.write(message + "\n");
  process.exitCode = 1;
});
