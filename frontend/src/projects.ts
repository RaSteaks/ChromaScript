import { call } from "./api";
import type { Project } from "./types";

/** Both project pickers consume every bounded API page before replacing their list. */
export async function loadProjects(
  signal?: AbortSignal,
): Promise<{ items: Project[] }> {
  const projects = new Map<string, Project>();
  let offset = 0;
  let total: number;
  do {
    signal?.throwIfAborted();
    const page = await call<{ items: Project[]; total: number }>(
      "list_projects",
      { limit: 200, offset },
      signal,
    );
    signal?.throwIfAborted();
    total = page.total;
    if (
      !Number.isInteger(total) ||
      total < 0 ||
      (!page.items.length && offset < total)
    )
      throw new Error("项目列表未能完整读取，请重新连接后重试。");
    // Concurrent inserts may shift offset pages. Deduplicate without losing the
    // selected project's identity; a failed page never publishes a partial list.
    for (const project of page.items) projects.set(project.id, project);
    offset += page.items.length;
  } while (offset < total);
  return { items: [...projects.values()] };
}
