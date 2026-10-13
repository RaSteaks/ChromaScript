import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Box,
  Copy,
  Download,
  Film,
  RefreshCw,
  Save,
  Square,
} from "lucide-react";
import { ApiError, artifactUrl, call, connect } from "./api";
import { Button, Field, Modal, Notice } from "./components";
import { CodeEditor } from "./CodeEditor";
import { DeliveryDestination } from "./DeliveryDestination";
import { OpenExportFolder } from "./ExportFolders";
import { MotionPreview } from "./MotionPreview";
import type {
  Artifact,
  Capability,
  Job,
  MotionCapabilities,
  MotionReply,
  MotionSpec,
  Project,
  SceneRecord,
} from "./types";

type Draft = {
  name: string;
  code: string;
  width: string;
  height: string;
  frames: string;
  fps: string;
};
type Page<T> = { items: T[]; total: number };
type Template = { id: string; name: string; spec: MotionSpec };
const pageSize = 20;
const rates = [
  "24/1",
  "25/1",
  "30/1",
  "50/1",
  "60/1",
  "24000/1001",
  "30000/1001",
  "60000/1001",
];
const statusLabel: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const stageLabel: Record<string, string> = {
  queued: "排队",
  starting: "启动浏览器",
  compiling: "编译代码",
  rendering: "渲染画面",
  encoding: "编码视频",
  exporting: "保存文件",
  completed: "导出完成",
};
const text = (cause: unknown) =>
  cause instanceof Error ? cause.message : String(cause);
const fromSpec = (spec: MotionSpec): Draft => ({
  name: spec.name,
  code: spec.code,
  width: String(spec.width),
  height: String(spec.height),
  frames: String(spec.frames),
  fps: `${spec.fps.numerator}/${spec.fps.denominator}`,
});

function validate(draft: Draft | null): {
  spec: MotionSpec | null;
  error: string;
  field: string;
} {
  if (!draft) return { spec: null, error: "", field: "" };
  if (!draft.name.trim() || draft.name.length > 120)
    return { spec: null, error: "请输入 1–120 字的动画名称。", field: "name" };
  for (const key of ["width", "height", "frames"] as const) {
    const value = Number(draft[key]);
    const max = key === "frames" ? 36000 : 4096;
    const min = key === "frames" ? 1 : 16;
    if (
      !/^\d+$/.test(draft[key]) ||
      !Number.isSafeInteger(value) ||
      value < min ||
      value > max ||
      (key !== "frames" && value % 2)
    )
      return {
        spec: null,
        error:
          key === "frames"
            ? "帧数必须是 1–36000 的整数。"
            : "画面宽、高必须是 16–4096 的偶数。",
        field: key,
      };
  }
  const [numerator, denominator] = draft.fps.split("/").map(Number);
  if (
    !/^\d+\/\d+$/.test(draft.fps) ||
    numerator < 1 ||
    numerator > 240000 ||
    denominator < 1 ||
    denominator > 100000 ||
    numerator / denominator < 1 ||
    numerator / denominator > 240
  )
    return { spec: null, error: "请选择有效帧率。", field: "fps" };
  if (!draft.code.trim() || draft.code.length > 200000)
    return {
      spec: null,
      error: "代码不能为空，最多支持 200000 字符。",
      field: "code",
    };
  return {
    spec: {
      schema_version: 1,
      name: draft.name,
      code: draft.code,
      width: Number(draft.width),
      height: Number(draft.height),
      frames: Number(draft.frames),
      fps: { numerator, denominator },
    },
    error: "",
    field: "",
  };
}

/** An independent editing session preserves browser source and ACES source separately. */
export function MotionWorkbench({
  active,
  onWorkspace,
}: {
  active: boolean;
  onWorkspace: () => void;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectLoad, setProjectLoad] = useState<
    "loading" | "loaded" | "failed"
  >("loading");
  const [loadError, setLoadError] = useState("");
  const [projectId, setProjectId] = useState("");
  const [scenes, setScenes] = useState<SceneRecord[]>([]);
  const [scenePage, setScenePage] = useState(0);
  const [sceneTotal, setSceneTotal] = useState(0);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [caps, setCaps] = useState<MotionCapabilities | null>(null);
  const [destinationOptions, setDestinationOptions] =
    useState<Capability["delivery_export"]>();
  const [destination, setDestination] = useState<string | null>(null);
  const [destinationError, setDestinationError] = useState("");
  const [directoryPicking, setDirectoryPicking] = useState(false);
  const [saved, setSaved] = useState<MotionReply | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const [composing, setComposing] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [pending, setPending] = useState("");
  const [conflict, setConflict] = useState(false);
  const [discard, setDiscard] = useState<{
    action: () => void;
    label: string;
  } | null>(null);
  const [history, setHistory] = useState<Job[]>([]);
  const [jobPage, setJobPage] = useState(0);
  const [jobTotal, setJobTotal] = useState(0);
  const [jobId, setJobId] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [refresh, setRefresh] = useState(0);
  const busy = useRef(false);
  const submission = useRef<{ recipe: string; key: string } | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const valid = useMemo(() => validate(draft), [draft]);
  const dirty =
    !!draft &&
    !!saved &&
    JSON.stringify(draft) !== JSON.stringify(fromSpec(saved.scene));
  const directory = destination ?? destinationOptions?.default_directory ?? "";

  const run = useCallback(
    async <T,>(
      label: string,
      action: () => Promise<T>,
    ): Promise<T | undefined> => {
      if (busy.current) return;
      busy.current = true;
      setPending(label);
      setError("");
      setStatus("");
      try {
        return await action();
      } catch (cause) {
        setError(text(cause));
        if (cause instanceof ApiError && cause.code === "revision_conflict")
          setConflict(true);
      } finally {
        busy.current = false;
        setPending("");
      }
    },
    [],
  );

  useEffect(() => {
    if (active) heading.current?.focus();
    // Revalidate on returning from project creation without replacing any draft.
    if (!active) return;
    let stopped = false;
    setProjectLoad("loading");
    // Discovery calls are independent: missing animation routes on an old
    // daemon must not erase a successfully loaded project list or an editor draft.
    void Promise.allSettled([
      call<Page<Project>>("list_projects", { limit: 200 }),
      call<{ items: Template[] }>("list_motion_templates"),
      call<MotionCapabilities>("get_motion_capabilities"),
      call<NonNullable<Capability["delivery_export"]>>(
        "get_delivery_export_options",
      ),
    ])
      .then(([projectReply, templateReply, capabilities, paths]) => {
        if (stopped) return;
        const failures: string[] = [];
        if (projectReply.status === "fulfilled") {
          const writable = projectReply.value.items.filter(
            (project) => !project.archived,
          );
          setProjects(writable);
          setProjectId((old) => old || writable[0]?.id || "");
          setProjectLoad("loaded");
        } else {
          setProjectLoad("failed");
          failures.push(text(projectReply.reason));
        }
        if (templateReply.status === "fulfilled")
          setTemplates(templateReply.value.items);
        else failures.push(text(templateReply.reason));
        if (capabilities.status === "fulfilled") setCaps(capabilities.value);
        else {
          setCaps(null);
          failures.push(text(capabilities.reason));
        }
        if (paths.status === "fulfilled") setDestinationOptions(paths.value);
        else failures.push(text(paths.reason));
        // Clear only discovery errors on recovery; save/conflict errors remain
        // owned by the explicit action that produced them.
        setLoadError([...new Set(failures)].join(" "));
      })
      .catch((cause) => {
        if (!stopped) {
          setProjectLoad("failed");
          setLoadError(text(cause));
        }
      });
    return () => {
      stopped = true;
    };
  }, [active, refresh]);

  useEffect(() => {
    if (!active || !projectId || !caps) return;
    let stopped = false;
    void call<Page<SceneRecord>>("list_motion_scenes", {
      project_id: projectId,
      limit: pageSize,
      offset: scenePage * pageSize,
    })
      .then((reply) => {
        if (!stopped) {
          setScenes(reply.items);
          setSceneTotal(reply.total);
        }
      })
      .catch((cause) => {
        if (!stopped) setError(text(cause));
      });
    return () => {
      stopped = true;
    };
  }, [active, projectId, scenePage, refresh, caps]);

  useEffect(() => {
    if (!active || !projectId || !caps) return;
    let stopped = false;
    void call<Page<Job>>("list_jobs", {
      project_id: projectId,
      kind: "motion",
      limit: pageSize,
      offset: jobPage * pageSize,
    })
      .then((reply) => {
        if (!stopped) {
          setHistory(reply.items);
          setJobTotal(reply.total);
        }
      })
      .catch((cause) => {
        if (!stopped) setError(text(cause));
      });
    return () => {
      stopped = true;
    };
  }, [active, projectId, jobPage, refresh, caps]);

  useEffect(() => {
    if (!active || !jobId) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const reply = await call<{ job: Job; artifacts: Artifact[] }>(
          "get_job",
          { job_id: jobId },
        );
        if (stopped) return;
        setJob(reply.job);
        setArtifacts(reply.artifacts);
        setHistory((old) =>
          old.map((item) => (item.id === jobId ? reply.job : item)),
        );
        if (["queued", "running"].includes(reply.job.status))
          timer = window.setTimeout(() => void poll(), 1000);
      } catch (cause) {
        if (!stopped) setError(text(cause));
      }
    };
    void poll();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [active, jobId, refresh]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  function guard(label: string, action: () => void) {
    if (dirty) setDiscard({ label, action });
    else action();
  }
  function select(reply: MotionReply) {
    setSaved(reply);
    setDraft(fromSpec(reply.scene));
    setConflict(false);
    setError("");
  }
  async function open(sceneId: string) {
    const reply = await run("读取动画", () =>
      call<MotionReply>("get_motion_scene", { scene_id: sceneId }),
    );
    if (reply) select(reply);
  }
  async function create(templateId: string) {
    const reply = await run("创建动画", () =>
      call<MotionReply>("create_motion_scene", {
        project_id: projectId,
        template_id: templateId,
      }),
    );
    if (reply) {
      select(reply);
      setScenePage(0);
      setRefresh((old) => old + 1);
      setStatus("动画已创建，可以开始编辑。");
    }
  }
  function change(key: keyof Draft, value: string) {
    setDraft((old) => (old ? { ...old, [key]: value } : old));
  }
  async function save() {
    if (!saved || !valid.spec || !draft) return;
    const captured = draft;
    const reply = await run("保存动画", () =>
      call<MotionReply>("update_motion_scene", {
        scene_id: saved.record.id,
        spec: valid.spec,
        expected_revision_id: saved.revision_id,
      }),
    );
    if (reply) {
      setSaved(reply);
      // Editing may continue while saving. A late acknowledgement never replaces newer text.
      if (draftRef.current === captured) setDraft(fromSpec(reply.scene));
      setConflict(false);
      setRefresh((old) => old + 1);
      setStatus("动画已保存。");
    }
  }
  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setStatus("已复制。");
    } catch {
      setError("复制未完成，请在代码编辑器中手动选择并复制。");
    }
  }
  async function render() {
    if (!saved || dirty || !valid.spec) return;
    const args = {
      scene_id: saved.record.id,
      expected_revision_id: saved.revision_id,
      export_directory: directory,
    };
    const recipe = JSON.stringify(args);
    if (submission.current?.recipe !== recipe)
      submission.current = { recipe, key: crypto.randomUUID() };
    const reply = await run("提交本地渲染", () =>
      call<{ job: Job }>("submit_motion_render", {
        ...args,
        idempotency_key: submission.current!.key,
      }),
    );
    if (reply) {
      setJobId(reply.job.id);
      setJob(reply.job);
      setArtifacts([]);
      setDestinationError("");
      setJobPage(0);
      setRefresh((old) => old + 1);
      setStatus("任务已提交，可继续编辑；渲染使用已保存版本。");
    } else {
      setDestinationError("请核对导出目录和上方错误后重试。");
      document.getElementById("motion-directory")?.focus();
    }
  }
  async function jobAction(operation: "cancel_job" | "resume_job") {
    const reply = await run(
      operation === "cancel_job" ? "取消动画渲染" : "重试动画渲染",
      () => call<{ job: Job }>(operation, { job_id: jobId }),
    );
    if (reply) {
      setJob(reply.job);
      setRefresh((old) => old + 1);
    }
  }
  const video = artifacts.find((item) => item.kind === "video");
  const report = artifacts.find((item) => item.kind === "report");
  const otherSelected =
    saved && !scenes.some((scene) => scene.id === saved.record.id)
      ? saved.record
      : null;

  return (
    <main hidden={!active} className="workbench motion-workbench">
      <div className="workbench-context">
        <div>
          <span className="tiny-label">REMOTION + THREE.JS</span>
          <h1 ref={heading} tabIndex={-1}>
            动画代码
          </h1>
          <p className="panel-description">
            在左侧写代码，右侧查看动画。保存后即可在本机导出视频。
          </p>
        </div>
        <span className={"save-state " + (dirty ? "unsaved" : "")}>
          {dirty
            ? "未保存"
            : saved
              ? "已保存 · " + saved.revision_id.slice(0, 8)
              : "选择或创建动画"}
        </span>
      </div>
      <Notice text={error} error />
      <Notice text={loadError} error />
      <div className="status-line" aria-live="polite">
        {pending
          ? pending + "…"
          : projectLoad === "loading"
            ? "正在读取动画项目…"
            : status}
      </div>
      <div className="motion-library">
        <Field label="动画项目" id="motion-project">
          <select
            id="motion-project"
            disabled={!!pending || projectLoad === "loading"}
            value={projectId}
            onChange={(event) => {
              const id = event.target.value;
              // Native and programmatic selection can reselect the current value;
              // clearing its list would not trigger a new project-dependent fetch.
              if (id === projectId) return;
              guard("切换项目", () => {
                setProjectId(id);
                setSaved(null);
                setDraft(null);
                setScenes([]);
                setScenePage(0);
                setJobPage(0);
                setJobId("");
                setJob(null);
                setHistory([]);
                setArtifacts([]);
              });
            }}
          >
            <option value="" disabled>
              {projectLoad === "failed" && !projects.length
                ? "项目列表暂不可用，请重试"
                : "选择项目"}
            </option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="已保存的动画" id="motion-scene">
          <select
            id="motion-scene"
            value={saved?.record.id || ""}
            disabled={!projectId || !!pending}
            onChange={(event) => {
              const id = event.target.value;
              if (id === saved?.record.id) return;
              guard("打开其他动画", () => void open(id));
            }}
          >
            <option value="" disabled>
              {scenes.length ? "选择动画" : "还没有动画，请从模板创建"}
            </option>
            {otherSelected && (
              <option value={otherSelected.id}>{otherSelected.name}</option>
            )}
            {scenes.map((scene) => (
              <option key={scene.id} value={scene.id}>
                {scene.name}
              </option>
            ))}
          </select>
        </Field>
        <div className="motion-template-actions">
          {templates.map((template) => (
            <Button
              key={template.id}
              disabled={!projectId || !!pending}
              onClick={() =>
                guard("创建新动画", () => void create(template.id))
              }
            >
              {template.id === "three" ? <Box size={15} /> : <Film size={15} />}
              新建{template.id === "three" ? "三维" : "文字"}动画
            </Button>
          ))}
          <Button
            aria-label="刷新动画列表与连接"
            disabled={!!pending}
            onClick={() => {
              void run("重新连接", async () => {
                await connect();
                setRefresh((old) => old + 1);
              });
            }}
          >
            <RefreshCw size={15} />
          </Button>
        </div>
      </div>
      {sceneTotal > pageSize && (
        <div className="pagination">
          <Button
            disabled={!scenePage || !!pending}
            onClick={() => setScenePage((old) => old - 1)}
          >
            上一页动画
          </Button>
          <span>
            {scenePage + 1} / {Math.ceil(sceneTotal / pageSize)}
          </span>
          <Button
            disabled={(scenePage + 1) * pageSize >= sceneTotal || !!pending}
            onClick={() => setScenePage((old) => old + 1)}
          >
            下一页动画
          </Button>
        </div>
      )}
      {projectLoad === "loaded" && !projects.length && (
        <div className="empty-state">
          <p>先创建一个项目，再开始制作动画。</p>
          <Button onClick={onWorkspace}>前往工作区创建项目</Button>
        </div>
      )}
      {caps && !caps.preview_available && (
        <Notice
          error
          text="动画依赖尚未安装，请在项目目录运行 ./install.sh 后重新连接。"
        />
      )}
      {draft && (
        <>
          <div className="motion-studio">
            <section
              className="motion-source-panel"
              aria-label="动画代码编辑器"
            >
              <div className="section-heading">
                <h2>动画代码</h2>
                <span className="tiny-label">Animation.tsx</span>
              </div>
              <Field
                label="动画名称"
                id="motion-name"
                invalid={valid.field === "name"}
              >
                <input
                  id="motion-name"
                  value={draft.name}
                  maxLength={120}
                  aria-invalid={valid.field === "name"}
                  onChange={(event) => change("name", event.target.value)}
                />
              </Field>
              <CodeEditor
                value={draft.code}
                onChange={(code) => change("code", code)}
                onComposing={setComposing}
              />
              <p id="motion-code-help" className="technical-note">
                导出默认 React 组件。用 useCurrentFrame() 驱动动画；Three.js
                场景使用 ThreeCanvas。Tab 键可离开编辑器。
              </p>
              <div className="motion-save-actions">
                <Button
                  className="primary"
                  onClick={() => void save()}
                  disabled={!dirty || !valid.spec || !!pending || composing}
                >
                  <Save size={15} />
                  保存动画
                </Button>
                <Button onClick={() => void copy(draft.code)}>
                  <Copy size={15} />
                  复制代码
                </Button>
                {conflict && (
                  <Button
                    onClick={() =>
                      guard("载入最新版本", () => void open(saved!.record.id))
                    }
                  >
                    载入最新版本
                  </Button>
                )}
              </div>
              <Notice text={valid.error} error />
              <details className="technical-note">
                <summary>代码约定与可用依赖</summary>
                <p>
                  可导入 {caps?.imports.join("、")}
                  。依赖均在本地安装，不支持网络资源或任意文件导入。避免使用
                  Date.now()、随机数或 useFrame() 累加动画；每一帧应由帧号决定。
                </p>
              </details>
            </section>
            <div className="motion-preview-column">
              <MotionPreview
                spec={valid.spec}
                sceneId={saved!.record.id}
                active={active}
                composing={composing}
              />
              <section className="motion-settings" aria-label="动画规格">
                <div className="section-heading">
                  <h3>画面与时间</h3>
                  <small>
                    {valid.spec
                      ? `${((valid.spec.frames * valid.spec.fps.denominator) / valid.spec.fps.numerator).toFixed(2)} 秒`
                      : "请完善规格"}
                  </small>
                </div>
                <div className="motion-spec-fields">
                  {(
                    [
                      ["width", "宽度"],
                      ["height", "高度"],
                      ["frames", "总帧数"],
                    ] as const
                  ).map(([key, label]) => (
                    <Field
                      key={key}
                      label={label}
                      id={"motion-" + key}
                      invalid={valid.field === key}
                    >
                      <input
                        id={"motion-" + key}
                        type="number"
                        min={key === "frames" ? 1 : 16}
                        max={key === "frames" ? 36000 : 4096}
                        step={key === "frames" ? 1 : 2}
                        value={draft[key]}
                        aria-invalid={valid.field === key}
                        onChange={(event) => change(key, event.target.value)}
                      />
                    </Field>
                  ))}
                  <Field label="帧率" id="motion-fps">
                    <select
                      id="motion-fps"
                      value={draft.fps}
                      onChange={(event) => change("fps", event.target.value)}
                    >
                      {!rates.includes(draft.fps) && (
                        <option value={draft.fps}>{draft.fps} fps</option>
                      )}
                      {rates.map((rate) => (
                        <option key={rate} value={rate}>
                          {rate} fps
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
              </section>
            </div>
          </div>
          <section className="motion-export" aria-label="动画视频导出">
            <div>
              <h2>本地视频导出</h2>
              <p className="panel-description">
                MP4 · H.264 · sRGB · 8-bit · 无音频。透明背景合成到黑色。
              </p>
            </div>
            <DeliveryDestination
              onPickingChange={setDirectoryPicking}
              variant="animation"
              options={destinationOptions}
              value={directory}
              error={destinationError}
              disabled={!!pending}
              onChange={(value) => {
                setDestination(value);
                setDestinationError("");
              }}
            />
            <div className="motion-export-actions">
              <Button
                className="primary"
                disabled={
                  dirty ||
                  directoryPicking ||
                  !valid.spec ||
                  !!pending ||
                  !caps?.render_available ||
                  !directory.trim()
                }
                onClick={() => void render()}
              >
                <Download size={15} />
                导出 MP4
              </Button>
              <span>
                {dirty
                  ? "请先保存当前动画。"
                  : !caps?.render_available
                    ? "本地导出需要 Chrome、FFmpeg 和动画依赖。"
                    : "后台渲染使用已保存版本，可继续编辑。"}
              </span>
            </div>
          </section>
        </>
      )}
      <section className="motion-results" aria-label="动画渲染成果">
        <div className="section-heading">
          <h2>渲染任务与成果</h2>
          <small>{jobTotal} 个任务</small>
        </div>
        {history.length > 0 && (
          <Field label="动画渲染任务" id="motion-job">
            <select
              id="motion-job"
              value={jobId}
              onChange={(event) => {
                setJobId(event.target.value);
                setJob(null);
                setArtifacts([]);
              }}
            >
              <option value="" disabled>
                选择任务
              </option>
              {job && !history.some((item) => item.id === job.id) && (
                <option value={job.id}>
                  {job.id.slice(0, 8)} · {statusLabel[job.status]}
                </option>
              )}
              {history.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.id.slice(0, 8)} · {statusLabel[item.status]} ·{" "}
                  {item.settings.width} × {item.settings.height}
                </option>
              ))}
            </select>
          </Field>
        )}
        {jobTotal > pageSize && (
          <div className="pagination">
            <Button
              disabled={!jobPage}
              onClick={() => setJobPage((old) => old - 1)}
            >
              上一页动画任务
            </Button>
            <span>
              {jobPage + 1} / {Math.ceil(jobTotal / pageSize)}
            </span>
            <Button
              disabled={(jobPage + 1) * pageSize >= jobTotal}
              onClick={() => setJobPage((old) => old + 1)}
            >
              下一页动画任务
            </Button>
          </div>
        )}
        {!jobId && (
          <p className="panel-description">
            导出后在这里查看进度、下载视频和验证报告。
          </p>
        )}
        {job && (
          <>
            <div className="motion-job-status">
              <strong>
                {statusLabel[job.status]} ·{" "}
                {stageLabel[job.stage] || statusLabel[job.status]}
              </strong>
              <span className="mono">
                {job.completed_frames} / {job.settings.frames} 帧 ·{" "}
                {job.settings.fps.numerator}/{job.settings.fps.denominator} fps
              </span>
              {["queued", "running"].includes(job.status) && (
                <Button
                  disabled={!!pending}
                  onClick={() => void jobAction("cancel_job")}
                >
                  <Square size={13} />
                  取消渲染
                </Button>
              )}
              {["failed", "cancelled", "interrupted"].includes(job.status) && (
                <Button
                  disabled={!!pending}
                  onClick={() => void jobAction("resume_job")}
                >
                  重试渲染
                </Button>
              )}
            </div>
            <progress aria-label="动画导出进度" value={job.progress} max={1} />
            <Notice text={job.error?.message || ""} error />
            <div className="motion-artifact-actions">
              {video && (
                <a href={artifactUrl(video.id, true)} className="button">
                  <Download size={15} />
                  下载 MP4
                </a>
              )}
              {report && (
                <a href={artifactUrl(report.id, true)} className="button">
                  下载验证报告
                </a>
              )}
              {job.export_completed && job.export_path && (
                <Button onClick={() => void copy(job.export_path!)}>
                  <Copy size={14} />
                  复制导出路径
                </Button>
              )}
              {job.export_completed &&
                job.status === "completed" &&
                job.export_path && (
                  <OpenExportFolder directory={job.export_path} />
                )}
            </div>
            {job.export_completed && (
              <p className="mono motion-path">{job.export_path}</p>
            )}
          </>
        )}
      </section>
      <Modal
        open={!!discard}
        title="保留当前动画草稿"
        onClose={() => setDiscard(null)}
      >
        <p>当前动画尚未保存。继续{discard?.label}会丢弃本地修改。</p>
        <div className="modal-actions">
          <Button autoFocus onClick={() => setDiscard(null)}>
            继续编辑
          </Button>
          <Button
            onClick={() => {
              const action = discard?.action;
              setDiscard(null);
              action?.();
            }}
          >
            丢弃修改并继续
          </Button>
        </div>
      </Modal>
    </main>
  );
}
