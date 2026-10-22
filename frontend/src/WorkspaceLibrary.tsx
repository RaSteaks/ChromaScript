import { useEffect, useState } from "react";
import { FolderPlus, Plus, RefreshCw } from "lucide-react";
import { call } from "./api";
import { Button, Field, Notice } from "./components";
import type { MotionSpec, Project, SceneRecord, Template } from "./types";

export type AuthoringEngine = "wgsl" | MotionSpec["engine"];
export type MotionTemplate = { id: string; name: string; spec: MotionSpec };

/** One library selects existing records without converting either revision format. */
export function WorkspaceLibrary({
  projects,
  projectId,
  scenes,
  motionRecord,
  selected,
  busy,
  templates,
  motionTemplates,
  onProject,
  onRefresh,
  onNewProject,
  onSelect,
  onCreate,
}: {
  projects: Project[];
  projectId: string;
  scenes: SceneRecord[];
  motionRecord: SceneRecord | null;
  selected: string;
  busy: boolean;
  templates: Template[];
  motionTemplates: MotionTemplate[];
  onRefresh: () => void;
  onProject: (id: string) => void;
  onNewProject: () => void;
  onSelect: (value: string) => void;
  onCreate: (engine: AuthoringEngine, template?: string) => void;
}) {
  const [engine, setEngine] = useState<AuthoringEngine>("wgsl");
  const [page, setPage] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [library, setLibrary] = useState<{
    project: string;
    items: SceneRecord[];
    total: number;
  }>({ project: "", items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const revision = motionRecord?.revision_id;
  useEffect(() => {
    setPage(0);
  }, [projectId]);
  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    // Selected off-page records stay pinned; late reads cannot relabel another project.
    void call<{ items: SceneRecord[]; total: number }>(
      "list_motion_scenes",
      {
        project_id: projectId,
        limit: 20,
        offset: page * 20,
      },
      controller.signal,
    )
      .then((reply) => {
        if (controller.signal.aborted) return;
        setLibrary({ ...reply, project: projectId });
        if (page > 0 && page * 20 >= reply.total)
          setPage(Math.max(0, Math.ceil(reply.total / 20) - 1));
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted)
          setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [projectId, page, refresh, revision]);
  const motions = library.project === projectId ? library.items : [];
  const pinned =
    motionRecord?.project_id === projectId &&
    !motions.some((s) => s.id === motionRecord.id)
      ? motionRecord
      : null;
  const examples =
    engine === "wgsl"
      ? templates
      : motionTemplates.filter((t) => t.spec.engine === engine);
  return (
    <section className="workspace-library" aria-label="项目与作品">
      <div className="workspace-library-controls">
        <Field label="当前项目" id="project-select">
          <select
            id="project-select"
            value={projectId}
            disabled={busy}
            onChange={(e) => onProject(e.target.value)}
          >
            <option value="">选择或新建项目</option>
            {projects
              .filter((p) => !p.archived)
              .map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
          </select>
        </Field>
        <Button disabled={busy} onClick={onNewProject}>
          <FolderPlus size={15} />
          新建项目
        </Button>
        <Field label="当前作品" id="workspace-scene">
          <select
            id="workspace-scene"
            value={selected}
            disabled={busy || !projectId}
            onChange={(e) => onSelect(e.target.value)}
          >
            <option value="" disabled>
              选择已有作品
            </option>
            <optgroup label="WGSL 场景">
              {scenes.map((s) => (
                <option key={s.id} value={`wgsl:${s.id}`}>
                  {s.name}
                </option>
              ))}
            </optgroup>
            <optgroup label="Three.js / React 动画">
              {[...(pinned ? [pinned] : []), ...motions].map((s) => (
                <option key={s.id} value={`motion:${s.id}`}>
                  {s.name}
                </option>
              ))}
            </optgroup>
          </select>
        </Field>
        <Button
          disabled={busy || !projectId}
          aria-label="刷新作品列表"
          onClick={() => {
            setRefresh((n) => n + 1);
            onRefresh();
          }}
        >
          <RefreshCw size={15} />
        </Button>
        <Field label="新建类型" id="workspace-engine">
          <select
            id="workspace-engine"
            value={engine}
            disabled={busy}
            onChange={(e) => setEngine(e.target.value as AuthoringEngine)}
          >
            <option value="wgsl">WGSL 着色器</option>
            <option value="three-float-v1">Three.js · 浮点 HDR / SDR</option>
            <option value="remotion-sdr-v1">React · SDR 网页动画</option>
          </select>
        </Field>
        <Button
          className="primary"
          disabled={busy || !projectId}
          onClick={() => onCreate(engine)}
        >
          <Plus size={15} />
          新建作品
        </Button>
      </div>
      <div className="library-status" role="status">
        {loading
          ? "正在读取作品…"
          : projectId
            ? "选择作品后，在同一个工作区编辑、预览与导出。"
            : "创建项目后，选择创作类型开始。"}
      </div>
      <Notice text={error} error />
      {library.project === projectId && library.total > 20 && (
        <div className="pagination">
          <Button
            disabled={busy || loading || page === 0}
            onClick={() => setPage((n) => n - 1)}
          >
            上一页动画
          </Button>
          <span>
            {page + 1} / {Math.ceil(library.total / 20)}
          </span>
          <Button
            disabled={busy || loading || (page + 1) * 20 >= library.total}
            onClick={() => setPage((n) => n + 1)}
          >
            下一页动画
          </Button>
        </div>
      )}
      <details className="technical-note">
        <summary>示例模板（可选）</summary>
        <div className="motion-template-actions">
          {examples.map((t) => (
            <Button
              key={t.id}
              disabled={busy || !projectId}
              onClick={() => onCreate(engine, t.id)}
            >
              新建{t.name}
            </Button>
          ))}
          {!examples.length && <span>可以直接新建空白作品。</span>}
        </div>
      </details>
    </section>
  );
}
