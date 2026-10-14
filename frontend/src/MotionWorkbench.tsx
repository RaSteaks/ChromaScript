import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Box,
  Copy,
  Download,
  Film,
  Plus,
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
  MotionOutput,
  MotionDeliveryOptions,
  Project,
  SceneRecord,
} from "./types";

type Draft = {
  engine: MotionSpec["engine"];
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
  engine: spec.engine,
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
      value > max
    )
      return {
        spec: null,
        error:
          key === "frames"
            ? "帧数必须是 1–36000 的整数。"
            : "画面宽、高必须是 16–4096 的整数。",
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
      engine: draft.engine,
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

/** Source and delivery drafts survive navigation; only display copies are sRGB. */
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
  const [blankEngine, setBlankEngine] =
    useState<MotionSpec["engine"]>("remotion-sdr-v1");
  const [templates, setTemplates] = useState<Template[]>([]);
  const [caps, setCaps] = useState<MotionCapabilities | null>(null);
  const [output, setOutput] = useState<MotionOutput>({
    master_bit_depth: 32,
    video: "pq",
  });
  const [outputError, setOutputError] = useState("");
  const [outputOptions, setOutputOptions] =
    useState<MotionDeliveryOptions | null>(null);
  const [destinationOptions, setDestinationOptions] =
    useState<Capability["delivery_export"]>();
  const [destination, setDestination] = useState<string | null>(null);
  const [destinationError, setDestinationError] = useState("");
  const [directoryPicking, setDirectoryPicking] = useState(false);
  const [saved, setSaved] = useState<MotionReply | null>(null);
  const savedRef = useRef(saved);
  savedRef.current = saved;
  const [draft, setDraft] = useState<Draft | null>(null);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const [composing, setComposing] = useState(false);
  const composingRef = useRef(false);
  const [syncError, setSyncError] = useState("");
  const [syncPaused, setSyncPaused] = useState(false);
  const [externalRevision, setExternalRevision] = useState("");
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
  const savedId = saved?.record.id;

  useEffect(() => {
    if (!active || !savedId || pending || composing || syncPaused) return;
    let stopped = false;
    let reading = false;
    let halted = false;
    let failures = 0;
    let generation = 0;
    let rescan = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;

    const poll = async () => {
      if (stopped || reading || document.hidden || busy.current) return;
      const baseline = savedRef.current;
      if (!baseline || baseline.record.id !== savedId) return;
      reading = true;
      rescan = false;
      const requestGeneration = generation;
      const abort = new AbortController();
      controller = abort;
      let delay = 1000;
      const isCurrent = () =>
        !stopped &&
        !abort.signal.aborted &&
        requestGeneration === generation &&
        !document.hidden &&
        !busy.current &&
        !composingRef.current &&
        savedRef.current?.record.id === savedId &&
        savedRef.current.record.project_id === projectId &&
        savedRef.current.revision_id === baseline.revision_id;
      try {
        const reply = await call<MotionReply>(
          "get_motion_scene",
          { scene_id: savedId },
          AbortSignal.any([abort.signal, AbortSignal.timeout(10000)]),
        );
        // Recheck the live baseline, not the state captured by an old render.
        // A save, selection, IME session or hidden tab invalidates this read.
        const currentSaved = savedRef.current;
        if (!isCurrent() || !currentSaved) return;
        failures = 0;
        setSyncError("");
        // A delayed library read may contain the old name even after the editor
        // accepts a revision. Repair only this selected record, without discovery.
        setScenes((old) =>
          old.some(
            (item) =>
              item.id === savedId && item.revision_id !== reply.revision_id,
          )
            ? old.map((item) => (item.id === savedId ? reply.record : item))
            : old,
        );
        if (reply.revision_id === currentSaved.revision_id) return;
        const currentDraft = draftRef.current;
        if (
          currentDraft &&
          JSON.stringify(currentDraft) !==
            JSON.stringify(fromSpec(currentSaved.scene))
        ) {
          // Keep the original expected_revision_id: adopting a remote baseline
          // under a local draft would allow a later save to silently overwrite it.
          setExternalRevision(reply.revision_id);
          setConflict(true);
          return;
        }
        savedRef.current = reply;
        draftRef.current = fromSpec(reply.scene);
        setSaved(reply);
        setDraft(draftRef.current);
        setExternalRevision("");
        setConflict(false);
        setStatus("已同步外部更新。");
      } catch (cause) {
        // Suspension is cancellation, while a timeout is a genuine failed read.
        // Sync owns its feedback and never clears explicit save/conflict errors.
        if (!isCurrent()) return;
        failures += 1;
        halted = failures >= 3;
        delay = failures === 1 ? 2000 : 4000;
        setSyncError(
          (halted
            ? "自动同步已暂停，代码和草稿已保留。请使用上方连接按钮重新连接。"
            : "暂时无法同步动画，代码和草稿已保留，将自动重试。") +
            " " +
            text(cause),
        );
        if (halted) setSyncPaused(true);
      } finally {
        reading = false;
        if (!stopped && !halted && !document.hidden)
          timer = window.setTimeout(() => void poll(), rescan ? 0 : delay);
      }
    };
    const visibility = () => {
      generation += 1;
      window.clearTimeout(timer);
      controller?.abort();
      rescan = !document.hidden;
      // If a suspended read is still settling, its finally schedules this scan;
      // otherwise read immediately on return. Never overlap active requests.
      if (rescan) void poll();
    };
    document.addEventListener("visibilitychange", visibility);
    void poll();
    return () => {
      stopped = true;
      controller?.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [active, savedId, projectId, pending, composing, syncPaused, refresh]);

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
        if (
          capabilities.status === "fulfilled" &&
          capabilities.value.contract === "motion-dual-v1"
        )
          setCaps(capabilities.value);
        else {
          setCaps(null);
          failures.push(
            capabilities.status === "rejected"
              ? text(capabilities.reason)
              : "请重启本地服务以启用 HDR/SDR 动画工作流。",
          );
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

  const engine = draft?.engine;
  useEffect(() => {
    setOutputOptions(null);
    setOutputError("");
    if (!engine) return;
    let cancelled = false;
    void call<MotionDeliveryOptions>("get_motion_delivery_options", { engine })
      .then((reply) => {
        if (cancelled) return;
        if (reply.contract !== "motion-dual-v1" || reply.engine !== engine)
          throw new Error("动画输出服务版本不匹配，请重启服务。");
        setOutputOptions(reply);
        setOutput((old) =>
          reply.videos.some((v) => v.value === old.video) &&
          (engine === "three-float-v1"
            ? old.master_bit_depth !== null
            : old.master_bit_depth === null)
            ? old
            : reply.default,
        );
      })
      .catch((cause) => {
        if (!cancelled) setOutputError(text(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [engine, refresh]);

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
    savedRef.current = reply;
    draftRef.current = fromSpec(reply.scene);
    setSaved(reply);
    setDraft(draftRef.current);
    setConflict(false);
    setExternalRevision("");
    setSyncError("");
    setSyncPaused(false);
    setError("");
  }
  async function open(sceneId: string) {
    const reply = await run("读取动画", () =>
      call<MotionReply>("get_motion_scene", { scene_id: sceneId }),
    );
    if (reply) select(reply);
  }
  async function create(templateId: string | null = null) {
    const reply = await run("创建动画", () =>
      call<MotionReply>("create_motion_scene", {
        project_id: projectId,
        template_id: templateId,
        ...(templateId === null ? { engine: blankEngine } : {}),
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
    // Protect edits immediately, even if a background read settles before React
    // commits the input event. The same reference also guards late save replies.
    if (!draftRef.current) return;
    draftRef.current = { ...draftRef.current, [key]: value };
    setDraft(draftRef.current);
  }
  function compose(value: boolean) {
    composingRef.current = value;
    setComposing(value);
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
      savedRef.current = reply;
      setSaved(reply);
      // Editing may continue while saving. A late acknowledgement never replaces newer text.
      if (draftRef.current === captured) {
        draftRef.current = fromSpec(reply.scene);
        setDraft(draftRef.current);
      }
      setConflict(false);
      setExternalRevision("");
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
    if (
      !saved ||
      dirty ||
      !valid.spec ||
      outputOptions?.engine !== valid.spec.engine ||
      !outputOptions?.videos.find((item) => item.value === output.video)
        ?.available
    )
      return;
    const args = {
      scene_id: saved.record.id,
      expected_revision_id: saved.revision_id,
      export_directory: directory,
      output,
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
  const archive = artifacts.find((item) => item.kind === "master-archive");
  const thumbnail = artifacts.find((item) => item.kind === "preview-image");
  const report = artifacts.find((item) => item.kind === "report");
  const otherSelected =
    saved && !scenes.some((scene) => scene.id === saved.record.id)
      ? saved.record
      : null;

  return (
    <main
      hidden={!active}
      className="workbench motion-workbench"
      // IME protects metadata and canvas fields as well as the code editor.
      onCompositionStartCapture={() => compose(true)}
      onCompositionEndCapture={() => compose(false)}
    >
      <div className="workbench-context">
        <div className="context-title">
          <h1 ref={heading} tabIndex={-1}>
            动画代码
          </h1>
          <p className="panel-description">
            支持 HDR 浮点场景与 SDR 网页动画，统一使用 sRGB
            预览；按源类型选择母版与视频输出。
          </p>
        </div>
        <span className={"save-state " + (dirty ? "unsaved" : "")}>
          {dirty
            ? "未保存"
            : saved
              ? "已保存 · " + saved.revision_id.slice(0, 8)
              : ""}
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
                savedRef.current = null;
                draftRef.current = null;
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
              {scenes.length ? "选择动画" : "还没有动画，可新建空白动画"}
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
          <Field label="空白动画类型" id="motion-blank-engine">
            <select
              id="motion-blank-engine"
              value={blankEngine}
              disabled={!!pending}
              onChange={(event) =>
                setBlankEngine(event.target.value as MotionSpec["engine"])
              }
            >
              <option value="remotion-sdr-v1">SDR 网页</option>
              <option value="three-float-v1">浮点 HDR / SDR</option>
            </select>
          </Field>
          <Button
            className="primary"
            disabled={!projectId || !!pending}
            onClick={() => guard("新建空白动画", () => void create())}
          >
            <Plus size={15} />
            新建空白动画
          </Button>
          <Button
            aria-label="刷新动画列表与连接"
            disabled={!!pending}
            onClick={() => {
              void run("重新连接", async () => {
                await connect();
                setSyncPaused(false);
                setRefresh((old) => old + 1);
              });
            }}
          >
            <RefreshCw size={15} />
          </Button>
        </div>
      </div>
      <details className="technical-note">
        <summary>示例模板（可选）</summary>
        <p>
          这些示例只提供初始代码。通过 MCP
          创作时，可以直接创建空白动画或传入源码。
        </p>
        <div className="motion-template-actions">
          {templates.map((template) => (
            <Button
              key={template.id}
              className="outline"
              disabled={!projectId || !!pending}
              onClick={() =>
                guard("创建新动画", () => void create(template.id))
              }
            >
              {template.id === "three" ? <Box size={15} /> : <Film size={15} />}
              新建{template.name}
            </Button>
          ))}
        </div>
      </details>
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
      {/* Nothing is open yet: point at the controls above without duplicating them. */}
      {!draft && projectLoad === "loaded" && projects.length > 0 && (
        <div className="workflow-empty">
          <Film size={28} aria-hidden="true" />
          <h2>打开或新建一个动画</h2>
          <p>
            {scenes.length
              ? "在上方打开已保存的动画，或新建空白动画；示例模板可选。"
              : "这个项目还没有动画。选择渲染类型后点击“新建空白动画”，即可通过代码或 MCP 开始创作。"}
          </p>
        </div>
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
                <span className="tiny-label">
                  {draft.engine === "remotion-sdr-v1"
                    ? "Animation.tsx · SDR"
                    : "Animation.ts · HDR 浮点"}
                </span>
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
                onComposing={compose}
              />
              <p id="motion-code-help" className="technical-note">
                {draft.engine === "remotion-sdr-v1"
                  ? "SDR 网页动画：默认导出 React 组件，用 useCurrentFrame() 驱动时间；支持 DOM、SVG 与 ThreeCanvas。Tab 键可离开编辑器。"
                  : "浮点场景：默认导出工厂并返回 scene、camera、update。RGB 为线性 Rec.709，允许超过 1；母版转换为 ACEScg，视频可选 HDR 或 SDR。"}
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
              <Notice
                text={
                  externalRevision
                    ? "检测到外部更新，已保留未保存修改。可复制代码，或载入最新版本。"
                    : ""
                }
              />
              <Notice text={syncError} error />
              <Notice text={valid.error} error />
              <details className="technical-note">
                <summary>代码约定与可用依赖</summary>
                <p>
                  可导入 {caps?.engines[draft.engine].imports.join("、")}
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
                        step={1}
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
              <h2>动画导出</h2>
              <p className="panel-description">
                {draft.engine === "remotion-sdr-v1"
                  ? "SDR 网页源为 8-bit sRGB，可输出 sRGB 或 Rec.709 视频。"
                  : "保留 ACEScg EXR 浮点母版，可输出 SDR sRGB/Rec.709 或 HDR PQ/HLG 视频；预览始终为 sRGB。"}
              </p>
            </div>
            <Notice text={outputError} error />
            {/* Shared server discovery owns output choices; changing delivery never changes the preview. */}
            <div className="frame-settings">
              {draft.engine === "three-float-v1" && (
                <Field label="EXR 母版精度" id="motion-master-depth">
                  <select
                    id="motion-master-depth"
                    value={output.master_bit_depth ?? 32}
                    disabled={!!pending || !outputOptions}
                    onChange={(event) => {
                      // Capture before a discovery response rerenders the controlled select.
                      const depth = Number(event.currentTarget.value) as
                        16 | 32;
                      setOutput((old) => ({ ...old, master_bit_depth: depth }));
                    }}
                  >
                    {(outputOptions?.master_bit_depths || [16, 32]).map(
                      (depth) => (
                        <option key={depth} value={depth}>
                          {depth}-bit {depth === 32 ? "Float" : "Half"} · ACEScg
                        </option>
                      ),
                    )}
                  </select>
                </Field>
              )}
              <Field
                label="视频输出"
                id="motion-hdr-video"
                hint="视频要求偶数尺寸；可选格式由当前源类型决定。"
              >
                <select
                  id="motion-hdr-video"
                  value={output.video}
                  disabled={!!pending || !outputOptions}
                  onChange={(event) => {
                    const video = event.currentTarget
                      .value as MotionOutput["video"];
                    setOutput((old) => ({ ...old, video }));
                  }}
                >
                  {(outputOptions?.videos || []).map((item) => (
                    <option
                      key={item.value}
                      value={item.value}
                      disabled={!item.available}
                    >
                      {item.label}
                      {!item.available ? " · 本机缺少编码器" : ""}
                    </option>
                  ))}
                </select>
              </Field>
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
                  !directory.trim() ||
                  outputOptions?.engine !== draft.engine ||
                  !outputOptions?.videos.find(
                    (item) => item.value === output.video,
                  )?.available ||
                  (output.video !== "none" &&
                    !!valid.spec &&
                    (valid.spec.width % 2 !== 0 || valid.spec.height % 2 !== 0))
                }
                onClick={() => void render()}
              >
                <Download size={15} />
                导出动画
              </Button>
              <span>
                {dirty
                  ? "请先保存当前动画。"
                  : !caps?.render_available
                    ? "本地导出需要 Chrome、FFmpeg 和动画依赖。"
                    : output.video !== "none" &&
                        valid.spec &&
                        (valid.spec.width % 2 !== 0 ||
                          valid.spec.height % 2 !== 0)
                      ? "视频要求偶数尺寸；浮点场景可选择仅导出 EXR。"
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
            {thumbnail && (
              <img
                className="preview-image"
                src={artifactUrl(thumbnail.id)}
                alt="导出母版的 sRGB 预览"
              />
            )}
            <div className="motion-artifact-actions">
              {video && (
                <a href={artifactUrl(video.id, true)} className="button">
                  <Download size={15} />
                  下载视频 MP4
                </a>
              )}
              {archive && (
                // Native downloads stream to the browser's chosen location;
                // never fetch an entire EXR archive into an in-memory Blob.
                <a href={artifactUrl(archive.id, true)} className="button">
                  下载 ACEScg 母版 ZIP
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
            {archive?.representation === "zip-stream" && (
              <p className="panel-description">
                母版 ZIP
                在下载时生成，不额外占用导出盘空间；下载中断后需重新开始。
              </p>
            )}
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
