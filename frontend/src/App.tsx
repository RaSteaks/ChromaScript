import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import {
  Activity,
  ArrowDownToLine,
  Braces,
  Check,
  ChevronRight,
  Code2,
  Copy,
  FileImage,
  Film,
  FolderPlus,
  Layers,
  Play,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  Square,
  Terminal,
  Workflow,
} from "lucide-react";
import { artifactUrl, call, connect } from "./api";
import { DeliveryEditor } from "./DeliveryEditor";
import { useDeliveryTargets } from "./delivery";
import { Button, Field, Modal, Notice, Textarea } from "./components";
import type {
  Artifact,
  Capability,
  Job,
  Operation,
  Parameter,
  Project,
  Scene,
  SceneRecord,
  SceneReply,
  Settings,
  Template,
} from "./types";

const STATUS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const fmt = (value: number) =>
  Number.isFinite(value)
    ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)
    : "—";
const bytes = (value: number) => {
  // Small manifests should report their real size instead of rounding to zero MiB.
  if (value >= 1024 ** 3) return fmt(value / 1024 ** 3) + " GiB";
  if (value >= 1024 ** 2) return fmt(value / 1024 ** 2) + " MiB";
  if (value >= 1024) return fmt(value / 1024) + " KiB";
  return value + " B";
};
function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

export default function App() {
  const [tab, setTab] = useState("workspace");
  const [caps, setCaps] = useState<Capability | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [scenes, setScenes] = useState<SceneRecord[]>([]);
  const [sceneId, setSceneId] = useState("");
  const [saved, setSaved] = useState<SceneReply | null>(null);
  const [draft, setDraft] = useState<Scene | null>(null);
  const [templates, setTemplates] = useState<Template[]>([]);
  const delivery = useDeliveryTargets();
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobPage, setJobPage] = useState(0);
  const [jobTotal, setJobTotal] = useState(0);
  const [activeJobId, setActiveJobId] = useState("");
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [previewIndex, setPreviewIndex] = useState(0);
  const [showMotion, setShowMotion] = useState(false);
  const [stats, setStats] = useState<{
    minimum: number[];
    maximum: number[];
    high_rgb_values: number;
    negative_rgb_values: number;
  } | null>(null);
  const [pixel, setPixel] = useState<{
    x: number;
    y: number;
    values: number[];
  } | null>(null);
  const [width, setWidth] = useState("3840"),
    [height, setHeight] = useState("2160"),
    [frames, setFrames] = useState("300");
  const [fps, setFps] = useState("60/1"),
    [samples, setSamples] = useState("4"),
    [frame, setFrame] = useState("0");
  const [timecode, setTimecode] = useState("00:00:00:00");
  const [error, setError] = useState(""),
    [status, setStatus] = useState(""),
    [offline, setOffline] = useState(false);
  const [pending, setPending] = useState("");
  const submission = useRef<{ recipe: string; key: string } | null>(null);
  const busy = useRef(false),
    sampleRequest = useRef(0);
  const [projectModal, setProjectModal] = useState(false),
    [projectName, setProjectName] = useState(""),
    [nameError, setNameError] = useState("");
  const projectNameRef = useRef<HTMLInputElement>(null);
  const [discard, setDiscard] = useState<(() => void) | null>(null);
  const [toolName, setToolName] = useState(""),
    [toolArgs, setToolArgs] = useState("{}"),
    [toolResult, setToolResult] = useState("");
  const [exportArtifact, setExportArtifact] = useState<Artifact | null>(null);
  const [exportDestination, setExportDestination] = useState("");
  const [exportError, setExportError] = useState("");
  const [exportedPath, setExportedPath] = useState("");
  const [operations, setOperations] = useState<Operation[]>([]);
  const [estimate, setEstimate] = useState<Record<string, number> | null>(null);
  const dirty =
    !!draft && !!saved && JSON.stringify(draft) !== JSON.stringify(saved.scene);
  const activeProject = projects.find((p) => p.id === projectId);
  const activeJob = jobs.find((j) => j.id === activeJobId);
  const activeJobStatus = activeJob?.status;
  const previewImages = artifacts.filter((a) => a.kind === "preview-image");
  const preview = previewImages[previewIndex] || previewImages[0];
  const master = artifacts.find(
    (a) =>
      a.kind === "master-frame" && a.metadata.frame === preview?.metadata.frame,
  );
  const masterId = master?.id;
  const motion = artifacts.find((a) => {
    const video = (
      a.metadata.verification as
        { video?: { codec_name?: string; pix_fmt?: string } } | undefined
    )?.video;
    // Professional 10-bit/4:4:4 deliverables need their own player; the browser proxy is 8-bit 4:2:0.
    return (
      a.kind === "video" &&
      (a.name === "h264-review.mp4" ||
        (video?.codec_name === "h264" && video.pix_fmt === "yuv420p"))
    );
  });
  const frameRate = Number(fps.split("/")[0]) / Number(fps.split("/")[1]);
  const validFrameRates = (caps?.frame_rates || []).filter((rate) =>
    delivery.targets.every(
      (target) =>
        !target.resolved ||
        target.resolved.frame_rates.some(
          (item) =>
            item.numerator === rate.numerator &&
            item.denominator === rate.denominator,
        ),
    ),
  );
  const ratesKey = validFrameRates
    .map((rate) => `${rate.numerator}/${rate.denominator}`)
    .join(",");
  useEffect(() => {
    if (!delivery.ready || !ratesKey) return;
    const allowed = ratesKey.split(",");
    if (!allowed.includes(fps)) {
      const next = allowed.includes("60/1") ? "60/1" : allowed[0];
      setFps(next);
      setStatus("所选交付格式不支持原帧率，已联动调整为 " + next + " fps。");
    }
  }, [ratesKey, delivery.ready, fps]);

  const stale =
    saved &&
    scenes.find((s) => s.id === sceneId)?.revision_id !== saved.revision_id;

  const run = useCallback(
    async <T,>(label: string, action: () => Promise<T>): Promise<T | null> => {
      if (busy.current) return null;
      busy.current = true;
      setPending(label);
      setError("");
      setStatus("");
      try {
        const result = await action();
        setStatus(label + "已完成");
        setOffline(false);
        return result;
      } catch (e) {
        setError(errorText(e));
        return null;
      } finally {
        busy.current = false;
        setPending("");
      }
    },
    [],
  );

  const boot = useCallback(async () => {
    await connect();
    const [c, p, t, o] = await Promise.all([
      call<Capability>("get_capabilities"),
      call<{ items: Project[] }>("list_projects"),
      call<{ templates: Template[] }>("list_templates"),
      fetch("/api/operations").then((r) => r.json()) as Promise<{
        operations: Operation[];
      }>,
    ]);
    return { c, p, t, o };
  }, []);

  useEffect(() => {
    let stopped = false;
    boot()
      .then(({ c, p, t, o }) => {
        if (stopped) return;
        setCaps(c);
        setProjects(p.items);
        setTemplates(t.templates);
        setOperations(o.operations);
        setToolName(o.operations[0]?.name || "");
        setProjectId(p.items[0]?.id || "");
      })
      .catch((e) => {
        if (!stopped) {
          setOffline(true);
          setError(errorText(e));
        }
      });
    return () => {
      stopped = true;
    };
  }, [boot]);

  useEffect(() => {
    if (!projectId) {
      setScenes([]);
      setSceneId("");
      setSaved(null);
      setDraft(null);
      return;
    }
    const controller = new AbortController();
    call<{ scenes: SceneRecord[] }>(
      "get_project",
      { project_id: projectId },
      controller.signal,
    )
      .then((v) => {
        setScenes(v.scenes);
        setSceneId(v.scenes[0]?.id || "");
        setActiveJobId("");
        setJobPage(0);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorText(e));
      });
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    if (!sceneId) {
      setSaved(null);
      setDraft(null);
      setArtifacts([]);
      return;
    }
    const controller = new AbortController();
    setSaved(null);
    setDraft(null);
    setArtifacts([]);
    setPixel(null);
    call<SceneReply>("get_scene", { scene_id: sceneId }, controller.signal)
      .then((v) => {
        setSaved(v);
        setDraft(structuredClone(v.scene));
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorText(e));
      });
    return () => controller.abort();
  }, [sceneId]);

  useEffect(() => {
    const unload = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", unload);
    return () => window.removeEventListener("beforeunload", unload);
  }, [dirty]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    let reading = false;
    const poll = async () => {
      if (reading) return;
      reading = true;
      try {
        const value = await call<{ items: Job[]; total: number }>(
          "list_jobs",
          { project_id: projectId, limit: 8, offset: jobPage * 8 },
          controller.signal,
        );
        if (!controller.signal.aborted) {
          setJobs(value.items);
          setJobTotal(value.total);
          setOffline(false);
        }
      } catch (e) {
        if (!controller.signal.aborted) {
          setOffline(true);
          setError(errorText(e));
        }
      } finally {
        reading = false;
      }
    };
    void poll();
    const interval = window.setInterval(() => void poll(), 1500);
    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, [projectId, jobPage]);

  useEffect(() => {
    if (!activeJobId) {
      const completed = jobs.find(
        (j) => j.scene_id === sceneId && j.status === "completed",
      );
      if (completed) setActiveJobId(completed.id);
    }
  }, [jobs, sceneId, activeJobId]);

  useEffect(() => {
    if (!activeJobId) return;
    if (activeJobStatus && activeJobStatus !== "completed") return;
    const controller = new AbortController();
    call<{ artifacts: Artifact[] }>(
      "get_job",
      { job_id: activeJobId },
      controller.signal,
    )
      .then((v) => {
        setArtifacts(v.artifacts);
        const firstPreview = v.artifacts.find(
          (a) => a.kind === "preview-image",
        );
        if (firstPreview) setFrame(String(firstPreview.metadata.frame));
        setPreviewIndex(0);
        setShowMotion(false);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorText(e));
      });
    return () => controller.abort();
  }, [activeJobId, activeJobStatus]);

  useEffect(() => {
    sampleRequest.current++;
    setStats(null);
    setPixel(null);
    if (!masterId) return;
    const controller = new AbortController();
    call<typeof stats>(
      "inspect_frame",
      { artifact_id: masterId },
      controller.signal,
    )
      .then((v) => setStats(v))
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorText(e));
      });
    return () => controller.abort();
  }, [masterId]); // The stable artifact ID is the request identity.

  function guard(action: () => void) {
    if (dirty) setDiscard(() => action);
    else action();
  }
  function settings(): Settings {
    const w = Number(width),
      h = Number(height),
      n = Number(frames);
    if (
      !Number.isInteger(w) ||
      !Number.isInteger(h) ||
      w < 16 ||
      h < 16 ||
      w > 8192 ||
      h > 8192 ||
      !Number.isInteger(n) ||
      n < 1 ||
      n > 216000
    )
      throw new Error("尺寸需为 16–8192 的整数，帧数需为 1–216000。");
    const [numerator, denominator] = fps.split("/").map(Number);
    return {
      width: w,
      height: h,
      frames: n,
      fps: { numerator, denominator },
      samples: Number(samples),
      timecode,
    };
  }
  async function refreshProjects(selectedId?: string) {
    const p = await call<{ items: Project[] }>("list_projects");
    setProjects(p.items);
    if (selectedId) setProjectId(selectedId);
  }
  async function createProject() {
    if (!projectName.trim()) {
      setNameError("请输入项目名称。");
      projectNameRef.current?.focus();
      return;
    }
    const p = await run("创建项目", () =>
      call<{ project: Project }>("create_project", {
        name: projectName.trim(),
      }),
    );
    if (p) {
      await refreshProjects(p.project.id);
      setProjectModal(false);
      setProjectName("");
      setNameError("");
    }
  }
  async function createScene(templateId: string) {
    if (!projectId) {
      setProjectModal(true);
      return;
    }
    guard(() => {
      void run("创建场景", async () => {
        const s = await call<SceneReply>("create_scene", {
          project_id: projectId,
          template_id: templateId,
        });
        setScenes((old) => [s.record, ...old]);
        setSceneId(s.record.id);
        setActiveJobId("");
        return s;
      });
    });
  }
  async function saveScene() {
    if (!draft || !saved) return;
    const value = await run("保存场景", () =>
      call<SceneReply>("update_scene", {
        scene_id: sceneId,
        spec: draft,
        expected_revision_id: saved.revision_id,
      }),
    );
    if (value) {
      setSaved(value);
      setDraft(structuredClone(value.scene));
      setScenes((old) => old.map((s) => (s.id === sceneId ? value.record : s)));
    }
  }
  async function render(kind: "frame" | "preview" | "keyframes" | "delivery") {
    if (!sceneId || dirty) {
      setError("先保存场景修改，再生成预览或交付。");
      return;
    }
    await run(kind === "delivery" ? "提交交付" : "提交预览", async () => {
      const s = settings();
      const scale = Math.min(1, 960 / s.width, 720 / s.height);
      const small = {
        ...s,
        width: Math.max(16, Math.round((s.width * scale) / 2) * 2),
        height: Math.max(16, Math.round((s.height * scale) / 2) * 2),
      };
      let result: { job: Job };
      // Keep the same submission key after uncertain network outcomes.
      const recipe = JSON.stringify({
        kind,
        sceneId,
        revision: saved?.revision_id,
        settings: s,
        configurations: delivery.configurations,
        frame,
      });
      if (submission.current?.recipe !== recipe)
        submission.current = { recipe, key: crypto.randomUUID() };
      const idempotency_key = submission.current.key;
      const revision_id = saved?.revision_id;
      if (kind === "frame")
        result = await call("render_frame", {
          scene_id: sceneId,
          frame: Number(frame),
          width: small.width,
          height: small.height,
          samples: s.samples,
          fps: s.fps,
          idempotency_key,
          revision_id,
        });
      else if (kind === "keyframes")
        result = await call("render_keyframes", {
          scene_id: sceneId,
          settings: small,
          frame_indices: [
            ...new Set([0, Math.floor(s.frames / 2), s.frames - 1]),
          ],
          idempotency_key,
          revision_id,
        });
      else if (kind === "preview")
        result = await call("render_preview", {
          scene_id: sceneId,
          settings: {
            ...small,
            frames: Math.min(s.frames, Math.round(frameRate * 2)),
          },
          idempotency_key,
          revision_id,
        });
      else {
        if (!delivery.ready) throw new Error("请先完成有效的交付格式设置。");
        for (const configuration of delivery.configurations)
          await call("validate_delivery_configuration", {
            configuration,
            settings: s,
          });
        result = await call("submit_configured_delivery", {
          scene_id: sceneId,
          settings: s,
          configurations: delivery.configurations,
          idempotency_key,
          revision_id,
        });
      }
      submission.current = null;
      sampleRequest.current++;
      setArtifacts([]);
      setActiveJobId(result.job.id);
      setJobs((old) => [
        result.job,
        ...old.filter((j) => j.id !== result.job.id),
      ]);
      return result;
    });
  }
  async function sample(e: MouseEvent<HTMLButtonElement>) {
    if (!master || !preview || !activeJob || master.job_id !== activeJob.id)
      return;
    const img = e.currentTarget.querySelector("img");
    if (!img) return;
    const rect = img.getBoundingClientRect(),
      scale = Math.min(
        rect.width / img.naturalWidth,
        rect.height / img.naturalHeight,
      );
    const dw = img.naturalWidth * scale,
      dh = img.naturalHeight * scale;
    const localX =
      e.detail === 0 ? dw / 2 : e.clientX - rect.left - (rect.width - dw) / 2;
    const localY =
      e.detail === 0 ? dh / 2 : e.clientY - rect.top - (rect.height - dh) / 2;
    if (localX < 0 || localY < 0 || localX >= dw || localY >= dh) return;
    const x = Math.min(
      activeJob.settings.width - 1,
      Math.floor((localX / dw) * activeJob.settings.width),
    );
    const y = Math.min(
      activeJob.settings.height - 1,
      Math.floor((localY / dh) * activeJob.settings.height),
    );
    const request = ++sampleRequest.current;
    try {
      const v = await call<{
        pixels: { x: number; y: number; values: number[] }[];
      }>("sample_pixels", { artifact_id: master.id, points: [[x, y]] });
      if (request === sampleRequest.current) setPixel(v.pixels[0]);
    } catch (e) {
      setError(errorText(e));
    }
  }
  function parameterValue(p: Parameter) {
    return draft?.values[p.name] ?? p.default;
  }
  function changeParameter(name: string, value: unknown) {
    setDraft((old) =>
      old ? { ...old, values: { ...old.values, [name]: value } } : old,
    );
  }
  function chooseTool(name: string) {
    setToolName(name);
    setToolResult("");
    const schema = operations.find((o) => o.name === name)?.inputSchema;
    const properties = (schema?.properties ?? {}) as Record<
      string,
      Record<string, unknown>
    >;
    const required = (schema?.required ?? []) as string[];
    const args: Record<string, unknown> = {};
    for (const [key, property] of Object.entries(properties)) {
      if (property.default !== undefined) args[key] = property.default;
      if (required.includes(key)) {
        const known: Record<string, unknown> = {
          project_id: projectId,
          scene_id: sceneId,
          revision_id: saved?.revision_id,
          expected_revision_id: saved?.revision_id,
          settings: settings(),
          spec: draft,
          configurations: delivery.configurations,
          configuration: delivery.configurations[0],
          idempotency_key: crypto.randomUUID(),
          artifact_id: master?.id,
          job_id: activeJobId,
        };
        args[key] =
          known[key] ??
          (property.type === "string"
            ? ""
            : property.type === "array"
              ? []
              : {});
      }
    }
    setToolArgs(JSON.stringify(args, null, 2));
  }
  async function invokeTool() {
    const result = await run("运行接口", () =>
      call<Record<string, unknown>>(toolName, JSON.parse(toolArgs)),
    );
    if (result) {
      setToolResult(JSON.stringify(result, null, 2));
      await refreshProjects();
    }
  }
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setStatus("已复制");
    } catch {
      setError("复制未完成，请手动选择文本复制。");
    }
  }
  const defaultConfig = JSON.stringify(
    {
      mcpServers: {
        chromascript: {
          command: caps ? caps.executable : "/path/to/.venv/bin/chromascript",
          args: ["--workspace", caps?.workspace || "/path/to/workspace", "mcp"],
        },
      },
    },
    null,
    2,
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="wordmark">
          <Layers size={22} />
          <span>ChromaScript</span>
          <small>程序化影像工作台</small>
        </div>
        <nav aria-label="主要导航">
          {[
            ["workspace", "工作区"],
            ["mcp", "MCP 接口"],
          ].map(([id, label]) => (
            <Button
              key={id}
              className={tab === id ? "nav-active" : "nav-button"}
              aria-current={tab === id ? "page" : undefined}
              onClick={() => setTab(id)}
            >
              {label}
            </Button>
          ))}
        </nav>
        <div className="engine-state">
          <span className={"connection-dot " + (offline ? "offline" : "")} />
          {offline
            ? "服务未连接"
            : caps
              ? caps.gpu.backend_type + " · " + caps.gpu.device
              : "连接本地服务…"}
        </div>
      </header>
      <Notice text={error} error onClose={() => setError("")} />
      <div className="status-line" aria-live="polite">
        {pending ? pending + "…" : status}
      </div>
      {offline && (
        <div className="reconnect">
          <span>本地服务暂不可用，编辑内容已保留。</span>
          <Button
            onClick={() =>
              void run("重新连接", async () => {
                await connect();
                setOffline(false);
              })
            }
          >
            <RefreshCw size={14} />
            重新连接
          </Button>
        </div>
      )}
      {tab === "workspace" && (
        <div className="workbench">
          <aside className="scene-rail">
            <div className="section-heading">
              <span>项目与场景</span>
              <Button
                aria-label="新建项目"
                onClick={() => setProjectModal(true)}
              >
                <FolderPlus size={17} />
              </Button>
            </div>
            <Field label="当前项目" id="project-select">
              <select
                id="project-select"
                value={projectId}
                onChange={(e) => guard(() => setProjectId(e.target.value))}
              >
                <option value="">选择或新建项目</option>
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </Field>
            <div className="scene-list">
              {scenes.map((s) => (
                <Button
                  key={s.id}
                  className={
                    "scene-item " + (sceneId === s.id ? "selected" : "")
                  }
                  onClick={() =>
                    guard(() => {
                      setSceneId(s.id);
                      setActiveJobId("");
                    })
                  }
                >
                  <FileImage size={16} />
                  <span>{s.name}</span>
                  <ChevronRight size={13} />
                </Button>
              ))}
              {!scenes.length && (
                <p className="empty-small">从下方模板创建第一个场景。</p>
              )}
            </div>
            <div className="section-heading">
              <span>场景模板</span>
              <span className="count">{templates.length}</span>
            </div>
            <div className="template-list">
              {templates.map((t, i) => (
                <Button
                  key={t.id}
                  className="template-item"
                  onClick={() => void createScene(t.id)}
                  disabled={!!pending}
                >
                  <span className="template-symbol">
                    {
                      [
                        <Activity size={18} />,
                        <Layers size={18} />,
                        <Square size={18} />,
                        <Workflow size={18} />,
                        <Braces size={18} />,
                        <Activity size={18} />,
                      ][i]
                    }
                  </span>
                  <span>
                    <strong>{t.name}</strong>
                    <small>{t.description}</small>
                  </span>
                  <Plus size={13} />
                </Button>
              ))}
            </div>
            <div className="rail-footer">
              <span className="tiny-label">工作空间</span>
              <strong>ACEScg / AP1</strong>
              <small>
                float32 · 线性光运算
                <br />
                ACES 2.0 · 固定配置
              </small>
            </div>
          </aside>
          <main className="canvas-column">
            <div className="canvas-heading">
              <div>
                <span className="tiny-label">SCENE / 场景</span>
                <h1>{draft?.name || "构建你的第一段画面"}</h1>
              </div>
              <span className={"save-state " + (dirty ? "unsaved" : "")}>
                {dirty
                  ? "未保存"
                  : saved
                    ? "版本 " + saved.revision_id.slice(0, 8)
                    : "等待场景"}
              </span>
            </div>
            <div className="preview-toolbar">
              <div className="segmented">
                <Button
                  className={!showMotion ? "selected" : ""}
                  onClick={() => setShowMotion(false)}
                >
                  <FileImage size={14} />
                  关键帧
                </Button>
                <Button
                  disabled={!motion}
                  className={showMotion ? "selected" : ""}
                  onClick={() => setShowMotion(true)}
                >
                  <Film size={14} />
                  运动预览
                </Button>
              </div>
              <span>
                sRGB 显示预览{" "}
                <span className="dim">
                  {dirty
                    ? " / 修改尚未渲染"
                    : activeJob &&
                        saved &&
                        activeJob.revision_id !== saved.revision_id
                      ? " / 上一版本"
                      : " / 原始值见下方"}
                </span>
              </span>
            </div>
            <div
              className="preview-stage"
              aria-busy={activeJob?.status === "running"}
            >
              {showMotion && motion ? (
                <video
                  src={artifactUrl(motion.id)}
                  controls
                  preload="metadata"
                  aria-label="SDR 运动预览"
                />
              ) : preview ? (
                <button
                  type="button"
                  className="sample-surface"
                  onClick={(e) => void sample(e)}
                  aria-label="在预览中取样，按回车可取中心像素"
                >
                  <img
                    src={artifactUrl(preview.id)}
                    alt={draft?.name + "的 sRGB 显示预览"}
                  />
                </button>
              ) : (
                <div className="preview-empty">
                  <div className="preview-mark">
                    <Braces size={36} />
                  </div>
                  <strong>
                    {draft ? "场景代码已就绪" : "画面从一段代码开始"}
                  </strong>
                  <p>
                    {draft
                      ? "渲染关键帧，查看经过 ACES 显示变换的结果。"
                      : "创建项目，选择模板，然后调整颜色与运动。"}
                  </p>
                  <Button
                    disabled={!draft || !!pending || dirty}
                    onClick={() => void render("frame")}
                  >
                    <Play size={14} />
                    渲染当前帧
                  </Button>
                </div>
              )}
              {activeJob &&
                ["queued", "running"].includes(activeJob.status) && (
                  <div className="preview-progress">
                    <span>
                      {STATUS[activeJob.status]} · {activeJob.stage}
                    </span>
                    <progress
                      max={1}
                      value={activeJob.progress}
                      aria-label="当前预览任务进度"
                    />
                  </div>
                )}
            </div>
            <div className="frame-strip">
              <div className="frame-buttons">
                {previewImages.map((a, i) => (
                  <Button
                    key={a.id}
                    className={i === previewIndex ? "selected" : ""}
                    onClick={() => {
                      setPreviewIndex(i);
                      setFrame(String(a.metadata.frame));
                      setShowMotion(false);
                    }}
                  >
                    帧 {String(a.metadata.frame)}
                  </Button>
                ))}
              </div>
              <label htmlFor="frame-index">当前帧</label>
              <input
                id="frame-index"
                type="number"
                min={0}
                value={frame}
                onChange={(e) => setFrame(e.target.value)}
              />
              <Button
                disabled={!draft || !!pending || dirty}
                onClick={() => void render("frame")}
                aria-label="渲染所选帧"
              >
                <RefreshCw size={14} />
              </Button>
            </div>
            <div className="pixel-readout">
              <div>
                <span className="tiny-label">原始 ACEScg 数值</span>
                <strong>
                  {pixel ? "像素 " + pixel.x + ", " + pixel.y : "点击预览取样"}
                </strong>
              </div>
              <div className="channels">
                {["R", "G", "B", "A"].map((c, i) => (
                  <span key={c}>
                    <b>{c}</b>
                    {pixel ? pixel.values[i]?.toFixed(6) : "—"}
                  </span>
                ))}
              </div>
              <div className="range-summary">
                <span>最小 / 最大</span>
                <strong>
                  {stats
                    ? fmt(Math.min(...stats.minimum.slice(0, 3))) +
                      " / " +
                      fmt(Math.max(...stats.maximum.slice(0, 3)))
                    : "— / —"}
                </strong>
              </div>
            </div>
            <section className="code-section">
              <div className="section-heading">
                <span>
                  <Code2 size={15} />
                  场景代码 <small>WGSL</small>
                </span>
                <div>
                  <Button
                    disabled={!draft || !!pending}
                    onClick={() =>
                      void run("验证代码", () =>
                        call("validate_scene", { scene_id: sceneId }),
                      )
                    }
                  >
                    <Check size={14} />
                    验证已保存版本
                  </Button>
                  <Button
                    disabled={!sceneId || !!pending}
                    onClick={() =>
                      guard(() => {
                        void run("重新载入", async () => {
                          const v = await call<SceneReply>("get_scene", {
                            scene_id: sceneId,
                          });
                          setSaved(v);
                          setDraft(structuredClone(v.scene));
                          return v;
                        });
                      })
                    }
                  >
                    <RefreshCw size={14} />
                    重新载入
                  </Button>
                  <Button
                    disabled={!draft || !dirty || !!pending}
                    onClick={() => void saveScene()}
                  >
                    <Save size={14} />
                    保存场景
                  </Button>
                </div>
              </div>
              {stale && (
                <Notice
                  error
                  text="场景已有其他客户端更新。请复制当前代码或重新载入后再保存。"
                />
              )}
              <label className="sr-only" htmlFor="scene-code">
                WGSL 场景代码
              </label>
              <Textarea
                id="scene-code"
                className="resize-none code-editor"
                spellCheck={false}
                disabled={!draft || !!pending}
                value={draft?.code || ""}
                onChange={(e) =>
                  setDraft((old) =>
                    old ? { ...old, code: e.target.value } : old,
                  )
                }
                aria-describedby="code-help"
              />
              <div className="code-footer">
                <span id="code-help">
                  shade(ctx) → 线性 ACEScg RGBA · 使用 ctx.time 与 ctx.seed
                </span>
                <Button
                  disabled={!draft}
                  onClick={() => void copy(draft?.code || "")}
                  aria-label="复制场景代码"
                >
                  <Copy size={13} />
                </Button>
              </div>
            </section>
          </main>
          <aside className="output-panel">
            <div className="section-heading">
              <span>
                <Settings2 size={16} />
                场景参数
              </span>
            </div>
            <div className="parameter-fields">
              {draft?.parameters.map((p) => (
                <Field
                  key={p.name}
                  label={p.label || p.name}
                  id={"param-" + p.name}
                  hint={
                    p.kind === "color"
                      ? p.color_space + " RGB"
                      : p.minimum !== null || p.maximum !== null
                        ? [p.minimum ?? "−∞", p.maximum ?? "∞"].join(" … ")
                        : undefined
                  }
                >
                  {p.kind === "boolean" ? (
                    <input
                      id={"param-" + p.name}
                      type="checkbox"
                      checked={Boolean(parameterValue(p))}
                      disabled={!!pending}
                      onChange={(e) =>
                        changeParameter(p.name, e.target.checked)
                      }
                    />
                  ) : p.kind === "color" ? (
                    <div className="rgb-inputs">
                      {["R", "G", "B"].map((c, i) => (
                        <label key={c}>
                          <span>{c}</span>
                          <input
                            id={i === 0 ? "param-" + p.name : undefined}
                            aria-label={(p.label || p.name) + " " + c}
                            type="number"
                            step="0.01"
                            value={String((parameterValue(p) as number[])[i])}
                            disabled={!!pending}
                            onChange={(e) => {
                              const value = [
                                ...(parameterValue(p) as number[]),
                              ];
                              value[i] = Number(e.target.value);
                              changeParameter(p.name, value);
                            }}
                          />
                        </label>
                      ))}
                    </div>
                  ) : (
                    <input
                      id={"param-" + p.name}
                      type="number"
                      step="0.1"
                      min={p.minimum ?? undefined}
                      max={p.maximum ?? undefined}
                      value={String(parameterValue(p))}
                      disabled={!!pending}
                      onChange={(e) =>
                        changeParameter(
                          p.name,
                          e.target.value === "" ? "" : Number(e.target.value),
                        )
                      }
                    />
                  )}
                </Field>
              ))}
              {!draft && (
                <p className="empty-small">选择场景后显示可调参数。</p>
              )}
            </div>
            <div className="section-heading divider">
              <span>输出规格</span>
              <span className="tiny-label">离线渲染</span>
            </div>
            <Field label="画面尺寸" id="resolution">
              <select
                id="resolution"
                value={
                  Object.entries(caps?.resolutions || {}).find(
                    ([, v]) =>
                      String(v[0]) === width && String(v[1]) === height,
                  )?.[0] || "custom"
                }
                onChange={(e) => {
                  const v = caps?.resolutions[e.target.value];
                  if (v) {
                    setWidth(String(v[0]));
                    setHeight(String(v[1]));
                  }
                }}
              >
                <option value="custom">自定义</option>
                {Object.entries(caps?.resolutions || {}).map(([label, v]) => (
                  <option key={label} value={label}>
                    {label} · {v[0]} × {v[1]}
                  </option>
                ))}
              </select>
            </Field>
            <div className="two-fields">
              <Field label="宽度" id="out-width">
                <input
                  id="out-width"
                  type="number"
                  min={16}
                  max={8192}
                  value={width}
                  onChange={(e) => setWidth(e.target.value)}
                />
              </Field>
              <Field label="高度" id="out-height">
                <input
                  id="out-height"
                  type="number"
                  min={16}
                  max={8192}
                  value={height}
                  onChange={(e) => setHeight(e.target.value)}
                />
              </Field>
            </div>
            <div className="two-fields">
              <Field label="帧率" id="out-fps">
                <select
                  id="out-fps"
                  value={fps}
                  onChange={(e) => setFps(e.target.value)}
                >
                  {validFrameRates.map((f) => (
                    <option
                      key={f.numerator + "/" + f.denominator}
                      value={f.numerator + "/" + f.denominator}
                    >
                      {fmt(f.numerator / f.denominator)} fps
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="总帧数" id="out-frames">
                <input
                  id="out-frames"
                  type="number"
                  min={1}
                  max={216000}
                  value={frames}
                  onChange={(e) => setFrames(e.target.value)}
                />
              </Field>
            </div>
            <div className="two-fields">
              <Field label="空间采样" id="out-samples">
                <select
                  id="out-samples"
                  value={samples}
                  onChange={(e) => setSamples(e.target.value)}
                >
                  {[1, 4, 16].map((n) => (
                    <option key={n} value={n}>
                      {n} 样本 / 像素
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="起始时间码" id="out-timecode">
                <input
                  id="out-timecode"
                  value={timecode}
                  onChange={(e) => setTimecode(e.target.value)}
                />
              </Field>
            </div>
            <div className="output-duration">
              <span>{fmt(Number(frames) / frameRate)} 秒</span>
              <span>
                {width} × {height} · {fmt(frameRate)} fps
              </span>
            </div>
            <DeliveryEditor delivery={delivery} disabled={!!pending} />
            <p className="technical-note">
              HDR 文件使用独立输出变换。当前网页提供 sRGB 预览，不替代校准监看。
            </p>
            {estimate && (
              <div className="estimate">
                <span>未压缩缓存上限</span>
                <strong>{bytes(estimate.cache_upper_bound_bytes)}</strong>
              </div>
            )}
            <div className="output-actions">
              <Button
                disabled={!draft || !!pending || !delivery.ready}
                onClick={() =>
                  void run("估算资源", async () => {
                    const v = await call<Record<string, number>>(
                      "estimate_delivery",
                      {
                        settings: settings(),
                        configurations: delivery.configurations,
                      },
                    );
                    setEstimate(v);
                    return v;
                  })
                }
              >
                估算资源
              </Button>
              <Button
                className="primary"
                disabled={!draft || dirty || !!pending || !delivery.ready}
                onClick={() => void render("delivery")}
              >
                <Play size={15} />
                提交交付 · {delivery.targets.length}
              </Button>
            </div>
            <div className="preview-actions">
              <Button
                disabled={!draft || dirty || !!pending}
                onClick={() => void render("keyframes")}
              >
                预览三帧
              </Button>
              <Button
                disabled={!draft || dirty || !!pending}
                onClick={() => void render("preview")}
              >
                前 2 秒动画
              </Button>
            </div>
          </aside>
          <section className="job-panel">
            <div className="section-heading">
              <span>
                <Activity size={16} />
                渲染队列 <small>{jobTotal} 个任务</small>
              </span>
              <div>
                <Button
                  disabled={jobPage === 0}
                  onClick={() => setJobPage((p) => p - 1)}
                >
                  上一页
                </Button>
                <span className="page-label">{jobPage + 1}</span>
                <Button
                  disabled={(jobPage + 1) * 8 >= jobTotal}
                  onClick={() => setJobPage((p) => p + 1)}
                >
                  下一页
                </Button>
              </div>
            </div>
            {!jobs.length ? (
              <p className="empty-small">
                提交预览或交付后，可在这里查看进度、成果和恢复任务。
              </p>
            ) : (
              <div className="jobs">
                {jobs.map((j) => (
                  <div
                    className={
                      "job-row " + (j.id === activeJobId ? "active" : "")
                    }
                    key={j.id}
                  >
                    <Button
                      className="job-identity"
                      onClick={() => {
                        sampleRequest.current++;
                        setArtifacts([]);
                        setActiveJobId(j.id);
                      }}
                    >
                      <Film size={16} />
                      <span>
                        {j.id.slice(0, 8)}
                        <small>
                          {j.settings.width} × {j.settings.height} ·{" "}
                          {j.frame_indices
                            ? `${j.frame_indices.length} 个关键帧`
                            : `${j.settings.frames} 帧`}
                        </small>
                      </span>
                    </Button>
                    <div className="job-progress">
                      <span>
                        {STATUS[j.status]} · {j.stage}
                      </span>
                      <progress
                        max={1}
                        value={j.progress}
                        aria-label={"任务 " + j.id.slice(0, 8) + " 进度"}
                      />
                    </div>
                    <span className="mono">{fmt(j.elapsed_seconds)} s</span>
                    <div className="job-controls">
                      {["queued", "running"].includes(j.status) ? (
                        <Button
                          disabled={!!pending}
                          onClick={() =>
                            void run("取消任务", () =>
                              call("cancel_job", { job_id: j.id }),
                            )
                          }
                        >
                          取消
                        </Button>
                      ) : ["failed", "interrupted", "cancelled"].includes(
                          j.status,
                        ) ? (
                        <Button
                          disabled={!!pending}
                          onClick={() =>
                            void run("继续任务", () =>
                              call("resume_job", { job_id: j.id }),
                            )
                          }
                        >
                          继续
                        </Button>
                      ) : (
                        <Check size={16} />
                      )}
                    </div>
                    {j.error && <p className="job-error">{j.error.message}</p>}
                  </div>
                ))}
              </div>
            )}
            {!!artifacts.length && (
              <div className="artifact-strip">
                {artifacts
                  .filter((a) =>
                    [
                      "video",
                      "report",
                      "source",
                      "master-frame",
                      "delivered-frame",
                      "sequence-manifest",
                    ].includes(a.kind),
                  )
                  .map((a) => (
                    <a key={a.id} href={artifactUrl(a.id, true)} download>
                      <ArrowDownToLine size={13} />
                      {a.name}
                      <small>{bytes(a.size_bytes)}</small>
                    </a>
                  ))}
                {artifacts
                  .filter((a) => a.kind === "image-sequence")
                  .map((a) => (
                    <Button
                      key={a.id}
                      disabled={!!pending}
                      onClick={() => {
                        // Copy the complete sequence through the shared managed-export operation.
                        setExportArtifact(a);
                        setExportDestination(
                          `${caps?.workspace}/exports/${a.job_id.slice(0, 8)}-${a.name}`,
                        );
                        setExportError("");
                        setError("");
                      }}
                    >
                      <ArrowDownToLine size={13} />
                      导出序列 {a.name}
                    </Button>
                  ))}
              </div>
            )}
            {exportedPath && (
              <div className="export-result" role="status">
                <span>
                  已导出至 <code>{exportedPath}</code>
                </span>
                <Button
                  aria-label="复制导出路径"
                  onClick={() => void copy(exportedPath)}
                >
                  <Copy size={14} />
                </Button>
              </div>
            )}
          </section>
        </div>
      )}
      {tab === "mcp" && (
        <main className="page-content">
          <div className="page-heading">
            <div>
              <span className="tiny-label">MODEL CONTEXT PROTOCOL</span>
              <h1>让模型直接创作与交付</h1>
              <p>
                {operations.length} 个类型化操作，共用相同场景、任务和验证数据。
              </p>
            </div>
            <Terminal size={30} />
          </div>
          <div className="mcp-grid">
            <section className="integration-card">
              <h2>本地 stdio 连接</h2>
              <p>客户端启动适配器；渲染任务由独立服务持续执行。</p>
              <pre>{defaultConfig}</pre>
              <Button onClick={() => void copy(defaultConfig)}>
                <Copy size={14} />
                复制配置
              </Button>
              <h2>模型创作流程</h2>
              <ol>
                <li>查询能力与模板，创建场景。</li>
                <li>编译代码，渲染关键帧与运动预览。</li>
                <li>查看图像，检查原始 ACEScg 数值。</li>
                <li>修改参数，提交多版本交付。</li>
                <li>检查报告与实际码流；需要时恢复任务。</li>
              </ol>
              <p className="technical-note">
                MCP 工具的参数位于 request 对象内。图像通过
                get_artifact(include_image: true) 返回原生图片。
              </p>
            </section>
            <section className="integration-card">
              <h2>接口调试</h2>
              <Field label="操作" id="operation">
                <select
                  id="operation"
                  value={toolName}
                  onChange={(e) => chooseTool(e.target.value)}
                >
                  {operations.map((o) => (
                    <option key={o.name}>{o.name}</option>
                  ))}
                </select>
              </Field>
              <p className="technical-note">
                {operations.find((o) => o.name === toolName)?.description}
              </p>
              <Field label="参数 JSON" id="operation-args">
                <Textarea
                  id="operation-args"
                  className="resize-none json-editor"
                  value={toolArgs}
                  onChange={(e) => setToolArgs(e.target.value)}
                  spellCheck={false}
                />
              </Field>
              <Button
                className="primary"
                disabled={!!pending}
                onClick={() => void invokeTool()}
              >
                <Play size={14} />
                运行接口
              </Button>
              <h3>结构化结果</h3>
              <pre
                id="operation-result"
                className="tool-result"
                aria-live="polite"
              >
                {toolResult.slice(0, 50000) || "运行后显示结果。"}
              </pre>
            </section>
          </div>
        </main>
      )}
      <footer className="app-footer">
        <span>{activeProject?.name || "本地工作区"}</span>
        <span>
          ACES 2.0 · {caps?.color.sha256.slice(0, 12) || "配置加载中"} · v
          {caps?.version || "0.1.0"}
        </span>
      </footer>
      <Modal
        open={projectModal}
        title="新建项目"
        onClose={() => setProjectModal(false)}
      >
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void createProject();
          }}
        >
          <Field
            label="项目名称"
            id="project-name"
            hint={nameError || "用于组织场景、渲染任务和交付文件。"}
            invalid={!!nameError}
          >
            <input
              ref={projectNameRef}
              id="project-name"
              value={projectName}
              aria-invalid={!!nameError}
              aria-describedby="project-name-help"
              onChange={(e) => {
                setProjectName(e.target.value);
                setNameError("");
              }}
            />
          </Field>
          <div className="modal-actions">
            <Button onClick={() => setProjectModal(false)}>取消</Button>
            <Button type="submit" className="primary" disabled={!!pending}>
              创建项目
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={!!discard}
        title="保留场景修改"
        onClose={() => setDiscard(null)}
      >
        <p>当前场景有未保存修改。切换会舍弃这些本地修改。</p>
        <div className="modal-actions">
          <Button className="primary" onClick={() => setDiscard(null)}>
            继续编辑
          </Button>
          <Button
            onClick={() => {
              const action = discard;
              setDiscard(null);
              action?.();
            }}
          >
            切换并舍弃
          </Button>
        </div>
      </Modal>
      <Modal
        open={!!exportArtifact}
        title="导出图像序列"
        onClose={() => setExportArtifact(null)}
      >
        <p>复制全部帧与序列说明到指定位置。已有文件不会被覆盖。</p>
        <Notice text={error} error />
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            if (!exportDestination.trim()) {
              setExportError("请输入导出目录。");
              return;
            }
            if (!exportArtifact) return;
            void run("导出成果", () =>
              call<{ destination: string }>("export_artifact", {
                artifact_id: exportArtifact.id,
                destination: exportDestination.trim(),
              }),
            ).then((result) => {
              if (result) {
                setExportedPath(result.destination);
                setExportArtifact(null);
              }
            });
          }}
        >
          <Field
            label="导出目录"
            id="export-destination"
            invalid={!!exportError}
            hint={exportError || "支持工作区及启动服务时配置的导出目录。"}
          >
            <input
              id="export-destination"
              value={exportDestination}
              aria-invalid={!!exportError}
              aria-describedby="export-destination-help"
              onChange={(e) => {
                setExportDestination(e.target.value);
                setExportError("");
              }}
            />
          </Field>
          <div className="modal-actions">
            <Button onClick={() => setExportArtifact(null)}>取消</Button>
            <Button type="submit" className="primary" disabled={!!pending}>
              复制序列
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
