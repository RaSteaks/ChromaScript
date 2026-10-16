// All author modules live in memory. Relative imports never fall through to disk.
import ts from "typescript";
import { posix } from "node:path";

const extensions = [".ts", ".tsx", ".js", ".jsx"];
const validPath = /^(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.(?:ts|tsx|js|jsx)$/;

export function sourceModules(spec, allowed, root, external = false) {
  const entry =
    spec.engine === "remotion-sdr-v1" ? "Animation.tsx" : "Animation.ts";
  const files = spec.files ?? {};
  const names = new Set(["animation.ts", "animation.tsx"]);
  const directories = new Map();
  let length = spec.code.length;
  if (Object.keys(files).length > 128)
    throw new Error("最多支持 128 个分章文件。");
  for (const [name, code] of Object.entries(files)) {
    if (
      !validPath.test(name) ||
      name.length > 180 ||
      names.has(name.toLowerCase())
    )
      throw new Error(`无效或重复的分章路径：${name}`);
    names.add(name.toLowerCase());
    // Distinct filenames must share exact directory spelling for portable exports.
    const parts = name.split("/");
    for (let depth = 1; depth < parts.length; depth++) {
      const directory = parts.slice(0, depth).join("/");
      const key = directory.toLowerCase();
      if (directories.has(key) && directories.get(key) !== directory)
        throw new Error(`分章目录大小写不一致：${directory}`);
      directories.set(key, directory);
    }
    if (typeof code !== "string" || !code.trim() || code.length > 200000)
      throw new Error(`分章 ${name} 不能为空，最多 200000 字符。`);
    length += code.length;
  }
  if (length > 2000000) throw new Error("全部源码合计最多 2000000 字符。");
  const sources = new Map([[entry, spec.code], ...Object.entries(files)]);

  function resolve(name, importer) {
    if (allowed.has(name)) return null;
    if (!name.startsWith("./") && !name.startsWith("../"))
      throw new Error(
        `${importer}: 不允许导入 ${name}；仅支持本场景相对路径与 ${[...allowed].join(", ")}。`,
      );
    const path = posix.normalize(posix.join(posix.dirname(importer), name));
    if (path.startsWith("../") || /[\\?#:]/.test(path))
      throw new Error(`${importer}: 分章路径越界或无效：${name}`);
    const candidates = [
      path,
      ...extensions.map((ext) => path + ext),
      ...extensions.map((ext) => path + "/index" + ext),
    ];
    const found = candidates.find((candidate) => sources.has(candidate));
    if (!found) throw new Error(`${importer}: 找不到分章文件 ${name}。`);
    return found;
  }

  // Inspect unused modules too, including type-only imports that esbuild erases.
  for (const [name, code] of sources) {
    const source = ts.createSourceFile(
      name,
      code,
      ts.ScriptTarget.Latest,
      true,
      /[jt]sx$/.test(name) ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );
    function check(node) {
      if (!node || !ts.isStringLiteral(node))
        throw new Error(`${name}: import 必须使用固定字符串路径。`);
      resolve(node.text, name);
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
          throw new Error(`${name}: 请使用静态 import；动画不允许 require()。`);
      }
      if (ts.isImportEqualsDeclaration(node))
        throw new Error(`${name}: 请使用标准 ES import。`);
      ts.forEachChild(node, visit);
    }
    visit(source);
  }

  return {
    name: "animation-source-modules",
    setup(plugin) {
      plugin.onResolve({ filter: /^chromascript:scene$/ }, () => ({
        path: entry,
        namespace: "scene",
      }));
      plugin.onLoad({ filter: /.*/, namespace: "scene" }, ({ path }) => ({
        contents: sources.get(path),
        loader: posix.extname(path).slice(1),
        resolveDir: root,
      }));
      plugin.onResolve({ filter: /.*/, namespace: "scene" }, (args) => {
        const path = resolve(args.path, args.importer);
        if (path) return { path, namespace: "scene" };
        // SDR export shares a single React/Remotion registry with the renderer.
        if (external) return { path: args.path, external: true };
        return plugin.resolve(args.path, { resolveDir: root, kind: args.kind });
      });
    },
  };
}
