import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** Fail before launching any browser or fixture that could create test projects. */
export default async function ensureIsolatedWorkspace() {
  const target = process.env.CHROMASCRIPT_URL;
  if (!target) throw new Error("CHROMASCRIPT_URL must identify an isolated test service.");
  const url = new URL(target);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))
    throw new Error("Browser tests require a local isolated service.");
  const response = await fetch(new URL("/health", url), { signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error("Test service health check failed.");
  const health = await response.json();
  const userWorkspace = fileURLToPath(new URL("../workspace", import.meta.url));
  const canonical = (path) => { try { return realpathSync(path); } catch { return path; } };
  if (!health.workspace || canonical(health.workspace) === canonical(userWorkspace))
    throw new Error("Refusing to create test data in the real ChromaScript workspace. Start a separate service with --workspace /tmp/chromascript-test and use its URL.");
}
