import assert from "node:assert/strict";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

// Compile the production loader for Node; these tests never launch a browser.
const bundle = await build({
  entryPoints: [fileURLToPath(new URL("../src/projects.ts", import.meta.url))],
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
});
const { loadProjects } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`
);
const originalFetch = globalThis.fetch;
after(() => {
  globalThis.fetch = originalFetch;
});

function serve(handler) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    if (url === "/api/session") return Response.json({ token: "test" });
    assert.equal(url, "/api/operations/list_projects");
    const args = JSON.parse(init.body);
    calls.push(args);
    return handler(args, init);
  };
  return calls;
}

test("both picker cutoffs are exceeded without losing old projects", async () => {
  const projects = Array.from({ length: 451 }, (_, index) => ({
    id: `project-${index}`,
  }));
  const calls = serve(({ offset, limit }) =>
    Response.json({
      items: projects.slice(offset, offset + limit),
      total: projects.length,
    }),
  );
  assert.deepEqual((await loadProjects()).items, projects);
  assert.deepEqual(
    calls.map((call) => call.offset),
    [0, 200, 400],
  );
  assert.ok(calls.every((call) => call.limit === 200));
});

test("empty project lists finish after one request", async () => {
  const calls = serve(() => Response.json({ items: [], total: 0 }));
  assert.deepEqual(await loadProjects(), { items: [] });
  assert.equal(calls.length, 1);
});

test("later-page errors reject instead of returning a truncated picker", async () => {
  serve(({ offset }) =>
    offset === 0
      ? Response.json({ items: [{ id: "first" }], total: 2 })
      : Response.json({ error: { message: "offline" } }, { status: 503 }),
  );
  await assert.rejects(loadProjects(), /offline/);
});

test("an incomplete empty page fails instead of looping forever", async () => {
  serve(() => Response.json({ items: [], total: 2 }));
  await assert.rejects(loadProjects(), /项目列表未能完整读取/);
});

test("shifted pages cannot duplicate project options", async () => {
  serve(({ offset }) =>
    Response.json(
      offset === 0
        ? { items: [{ id: "new" }, { id: "middle" }], total: 4 }
        : { items: [{ id: "middle" }, { id: "old" }], total: 4 },
    ),
  );
  assert.deepEqual(
    (await loadProjects()).items.map((p) => p.id),
    ["new", "middle", "old"],
  );
});

test("aborted discovery stops before requesting another page", async () => {
  const controller = new AbortController();
  const calls = serve(() => {
    controller.abort();
    return Response.json({ items: [{ id: "first" }], total: 500 });
  });
  await assert.rejects(loadProjects(controller.signal), { name: "AbortError" });
  assert.equal(calls.length, 1);
});
