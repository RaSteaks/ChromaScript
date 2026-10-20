import { MotionStorageEstimate } from "./MotionStorageEstimate";
import { ProjectAudio } from "./ProjectAudio";
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
import { loadProjects } from "./projects";
import { Button, Field, Modal, Notice, Textarea } from "./components";
import { MotionSourceEditor, sourceFilesError } from "./MotionSourceEditor";
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
  MotionSource,
  MotionSourceSpace,
  MotionOutput,
  MotionDeliveryOptions,
  MotionVerification,
  Project,
  SceneRecord,
} from "./types";

type Draft = {
  engine: MotionSpec["engine"];
  source_color_space: MotionSourceSpace;
  preserved_layers: MotionSource[];
  name: string;
  code: string;
  files: Record<string, string>;
  width: string;
  height: string;
  frames: string;
  fps: string;
};
type Page<T> = { items: T[]; total: number };
type Template = { id: string; name: string; spec: MotionSpec };
const pageSize = 20;
// FFmpeg's integer rational boundary; physical GPU/codec limits are checked at render.
const specificationMax = 2147483647;
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
  source_color_space: spec.source_color_space ?? "Linear Rec.709 (sRGB)",
  preserved_layers: spec.preserved_layers ?? [],
  name: spec.name,
  code: spec.code,
  files: spec.files ?? {},
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
    const max = specificationMax;
    const min = 1;
    if (
      !/^\d+$/.test(draft[key]) ||
      !Number.isSafeInteger(value) ||
      value < min ||
      value > max
    )
      return {
        spec: null,
        error:
          "尺寸和帧数须为 1–2147483647 的整数；实际导出受硬件和编码器限制。",
        field: key,
      };
  }
  const [numerator, denominator] = draft.fps.split("/").map(Number);
  if (
    !/^\d+\/\d+$/.test(draft.fps) ||
    numerator < 1 ||
    !Number.isSafeInteger(numerator) ||
    numerator > specificationMax ||
    denominator < 1 ||
    !Number.isSafeInteger(denominator) ||
    denominator > specificationMax
  )
    return {
      spec: null,
      error: "帧率须为正整数分子/分母，例如 24000/1001。",
      field: "fps",
    };
  if (!draft.code.trim() || draft.code.length > 200000)
    return {
      spec: null,
      error: "代码不能为空，最多支持 200000 字符。",
      field: "code",
    };
  const fileError = sourceFilesError(draft.code, draft.files);
  if (fileError) return { spec: null, error: fileError, field: "code" };
  return {
    spec: {
      schema_version: 1,
      engine: draft.engine,
      source_color_space: draft.source_color_space,
      preserved_layers: draft.preserved_layers,
      name: draft.name,
      code: draft.code,
      files: draft.files,
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
    useState<MotionSpec["engine"]>("three-float-v1");
  const [templates, setTemplates] = useState<Template[]>([]);
  const [caps, setCaps] = useState<MotionCapabilities | null>(null);
  const [output, setOutput] = useState<MotionOutput>({
    master_bit_depth: 32,
    video: "none",
    render_scale: 2,
  });
  const [masterDepth, setMasterDepth] = useState<16 | 32>(32);
  const outputEngine = useRef<MotionSpec["engine"]>("three-float-v1");
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
  const [layerIndex, setLayerIndex] = useState<number | null>(null);
  const [targetSpace, setTargetSpace] =
    useState<MotionSourceSpace>("Linear Rec.2020");
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
  const [reviewTarget, setReviewTarget] = useState<MotionReply | null>(null);
  const [reviewer, setReviewer] = useState("");
  const [reviewNotes, setReviewNotes] = useState("");
  const [reviewError, setReviewError] = useState("");
  const busy = useRef(false);
  const submission = useRef<{ recipe: string; key: string } | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const valid = useMemo(() => validate(draft), [draft]);
  const dirty =
    !!draft &&
    !!saved &&
    JSON.stringify(draft) !== JSON.stringify(fromSpec(saved.scene));
  const directory = destination ?? destinationOptions?.default_directory ?? "";
  // Remote revisions may remove retained layers; never leave an orphaned editor selection.
  const editingLayer =
    layerIndex !== null && draft?.preserved_layers[layerIndex]
      ? layerIndex
      : null;
  const source =
    editingLayer === null ? draft : draft?.preserved_layers[editingLayer];
  const sourceSpaces =
    caps?.engines["three-float-v1"].source_color_spaces ?? [];
  const spaceLabel = (value: MotionSourceSpace) =>
    sourceSpaces.find((item) => item.value === value)?.label ?? value;
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
        if (reply.revision_id === currentSaved.revision_id) {
          // Review metadata can change without changing the immutable animation.
          if (
            JSON.stringify(reply.color_review) !==
            JSON.stringify(currentSaved.color_review)
          ) {
            savedRef.current = reply;
            setSaved(reply);
          }
          return;
        }
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
    const projectRequest = new AbortController();
    setProjectLoad("loading");
    // Discovery calls are independent: missing animation routes on an old
    // daemon must not erase a successfully loaded project list or an editor draft.
    void Promise.allSettled([
      loadProjects(projectRequest.signal),
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
      projectRequest.abort();
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
        // SDR's null depth is not an explicit float video-only choice. Reset on
        // source-family changes, while reconnects preserve the current mode.
        const changedEngine = outputEngine.current !== engine;
        outputEngine.current = engine;
        if (changedEngine && reply.default.master_bit_depth !== null)
          setMasterDepth(reply.default.master_bit_depth);
        setOutput((old) =>
          !changedEngine &&
          reply.videos.some((v) => v.value === old.video) &&
          (engine === "three-float-v1"
            ? old.master_bit_depth !== null ||
              (reply.video_only === true && old.video !== "none")
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
    setLayerIndex(null);
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
  function change<K extends keyof Draft>(key: K, value: Draft[K]) {
    // Protect edits immediately, even if a background read settles before React
    // commits the input event. The same reference also guards late save replies.
    if (!draftRef.current) return;
    draftRef.current = { ...draftRef.current, [key]: value };
    setDraft(draftRef.current);
  }
  function changeSource(
    key: "code" | "files",
    value: string | Record<string, string>,
  ) {
    if (!draft) return;
    // Layer edits join the same revision/draft guard as the native foreground.
    if (editingLayer === null) {
      if (key === "code") change("code", value as string);
      else change("files", value as Record<string, string>);
    } else {
      change(
        "preserved_layers",
        draft.preserved_layers.map((layer, index) =>
          index === editingLayer ? { ...layer, [key]: value } : layer,
        ),
      );
    }
  }
  async function upgrade() {
    if (!saved || dirty || pending || composing) return;
    const captured = draftRef.current;
    const reply = await run("升级色彩管线", () =>
      call<MotionReply>("upgrade_motion_scene", {
        scene_id: saved.record.id,
        expected_revision_id: saved.revision_id,
        source_color_space: targetSpace,
      }),
    );
    if (reply) {
      // A late upgrade acknowledgement cannot discard name/time edits made while
      // the request was in flight. The existing conflict/reload flow owns recovery.
      if (draftRef.current !== captured) {
        setConflict(true);
        setExternalRevision(reply.revision_id);
        setStatus(
          "管线已升级；请求期间的本地修改已保留，请复制草稿或载入最新版本。",
        );
        return;
      }
      select(reply);
      setRefresh((old) => old + 1);
      setStatus(
        "色彩管线已升级，原有颜色与源码已保留。新内容使用目标线性空间。",
      );
    }
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
    // A new attestation changes the frozen report, even if source/output stay
    // identical. A retry of the same evidence still reuses its original request key.
    const recipe = JSON.stringify({ args, colorReview: saved.color_review });
    if (submission.current?.recipe !== recipe)
      submission.current = { recipe, key: crypto.randomUUID() };
    const reply = await run("提交本地渲染", () =>
      call<{ job: Job }>("submit_motion_render", {
        ...args,
        idempotency_key: submission.current!.key,
      }),
    );
    if (reply) {
      // Acknowledgement ends this request; the next delivery freezes current audio.
      // Keep the key on uncertain failures so a deliberate retry cannot duplicate jobs.
      submission.current = null;
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
  const verification = report?.metadata.verification as
    MotionVerification | undefined;
  async function recordReview() {
    if (!reviewTarget || pending || composing) return;
    if (!reviewer.trim() || !reviewNotes.trim()) {
      setReviewError("请填写审核人和对线性颜色、显示变换及 Alpha 的审核依据。");
      document
        .getElementById(
          !reviewer.trim() ? "motion-reviewer" : "motion-review-notes",
        )
        ?.focus();
      return;
    }
    // Freeze the reviewed revision when opening the dialog; a concurrent save
    // must fail with a conflict, never apply this attestation to different code.
    const target = reviewTarget;
    const reply = await run("记录源码审核", () =>
      call<MotionReply>("record_motion_color_review", {
        scene_id: target.record.id,
        expected_revision_id: target.revision_id,
        reviewer,
        notes: reviewNotes,
      }),
    );
    if (!reply) {
      setReviewError("审核记录未保存，请查看错误并核对最新源码后重试。");
      return;
    }
    if (savedRef.current?.revision_id === reply.revision_id) {
      savedRef.current = reply;
      setSaved(reply);
    }
    setReviewTarget(null);
    setStatus("当前修订的源码审核记录已保存。");
  }
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
      <div className="workbench-context motion-context">
        <div className="context-title">
          <h1 ref={heading} tabIndex={-1}>
            动画代码
          </h1>
          <p className="panel-description">
            支持 HDR 浮点场景与 SDR 网页动画；自动选择 720p HDR 视频或 sRGB
            预览，母版与视频输出独立设置。
          </p>
        </div>
        {/* Share the description row with feedback instead of reserving an empty toolbar row. */}
        <div className="motion-context-feedback">
          <span className={"save-state " + (dirty ? "unsaved" : "")}>
            {dirty
              ? "未保存"
              : saved
                ? "已保存 · " + saved.revision_id.slice(0, 8)
                : ""}
          </span>
          <div className="status-line" aria-live="polite">
            {pending
              ? pending + "…"
              : projectLoad === "loading"
                ? "正在读取动画项目…"
                : status}
          </div>
        </div>
      </div>
      <Notice text={error} error />
      <Notice text={loadError} error />
      <div className="motion-library">
        {/* Audio belongs to the project, so keep its action beside that picker. */}
        <div className="motion-project-controls">
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
          <ProjectAudio
            key={projectId}
            projectId={projectId}
            disabled={!!pending}
          />
        </div>
        {/* Refresh reads the library/connection; it is not part of creating an animation. */}
        <div className="motion-scene-controls">
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
          <Button
            title="刷新动画列表与连接"
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
            <RefreshCw size={15} aria-hidden="true" />
          </Button>
        </div>
        <div className="motion-create-controls">
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
              {draft.engine === "three-float-v1" && (
                <div className="frame-settings">
                  <p className="technical-note">
                    当前创作空间：{spaceLabel(draft.source_color_space)}
                    。创作与合成保持线性，PQ 仅在 HDR 预览或视频导出时编码。
                  </p>
                  <Field label="管线目标空间" id="motion-target-space">
                    <select
                      id="motion-target-space"
                      value={targetSpace}
                      disabled={!sourceSpaces.length || !!pending}
                      onChange={(event) =>
                        setTargetSpace(event.target.value as MotionSourceSpace)
                      }
                    >
                      {sourceSpaces.map((space) => (
                        <option key={space.value} value={space.value}>
                          {space.label}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Button
                    disabled={
                      dirty ||
                      !!pending ||
                      composing ||
                      !sourceSpaces.length ||
                      targetSpace === draft.source_color_space
                    }
                    onClick={() => void upgrade()}
                  >
                    升级色彩管线
                  </Button>
                  <p className="technical-note">
                    先保存再升级。旧源码保留为背景图层并在线性空间转换，新源码原生使用目标空间；图层之间不共享深度。扩大色域不会自动增加原画面的饱和度。
                  </p>
                  {!!draft.preserved_layers.length && (
                    <Field label="编辑图层" id="motion-source-layer">
                      <select
                        id="motion-source-layer"
                        value={editingLayer ?? "native"}
                        onChange={(event) =>
                          setLayerIndex(
                            event.target.value === "native"
                              ? null
                              : Number(event.target.value),
                          )
                        }
                      >
                        <option value="native">
                          新内容 · {spaceLabel(draft.source_color_space)}
                        </option>
                        {draft.preserved_layers.map((layer, index) => (
                          <option key={index} value={index}>
                            保留图层 {index + 1} ·{" "}
                            {spaceLabel(
                              layer.source_color_space ??
                                "Linear Rec.709 (sRGB)",
                            )}
                          </option>
                        ))}
                      </select>
                    </Field>
                  )}
                </div>
              )}
              <MotionSourceEditor
                key={`${saved!.record.id}:${editingLayer ?? "native"}`}
                engine={draft.engine}
                code={source?.code ?? draft.code}
                files={source?.files ?? {}}
                onCode={(code) => changeSource("code", code)}
                onFiles={(files) => changeSource("files", files)}
                onComposing={compose}
                disabled={!!pending || composing}
              />
              <p id="motion-code-help" className="technical-note">
                {draft.engine === "remotion-sdr-v1"
                  ? "SDR 网页动画：默认导出 React 组件，用 useCurrentFrame() 驱动时间；支持 DOM、SVG 与 ThreeCanvas。Tab 键可离开编辑器。"
                  : `浮点场景：默认导出工厂并返回 scene、camera、update。当前编辑图层的 RGB 为 ${spaceLabel(source?.source_color_space ?? draft.source_color_space)}，保留负值与高光；不在源码中编码 PQ。母版为线性 ACEScg。`}
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
                <Button
                  onClick={() =>
                    void copy(
                      Object.keys(draft.files).length ||
                        draft.preserved_layers.length
                        ? JSON.stringify(valid.spec ?? draft, null, 2)
                        : draft.code,
                    )
                  }
                >
                  <Copy size={15} />
                  {Object.keys(draft.files).length ||
                  draft.preserved_layers.length
                    ? "复制全部源码"
                    : "复制代码"}
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
              {draft.engine === "three-float-v1" && (
                <div className="technical-note">
                  <p>
                    {!dirty && saved?.color_review?.status === "reviewed"
                      ? `源码颜色契约已审核 · ${saved.color_review.reviewer}`
                      : "源码颜色契约未审核"}
                    。审核是对当前修订的人工记录，独立于浮点链路验证。
                  </p>
                  <Button
                    disabled={dirty || !!pending || composing}
                    onClick={() => {
                      setReviewTarget(saved);
                      setReviewError("");
                      setReviewNotes("");
                    }}
                  >
                    记录源码审核
                  </Button>
                </div>
              )}
            </section>
            <div className="motion-preview-column">
              <MotionPreview
                projectId={projectId}
                spec={valid.spec}
                sceneId={saved!.record.id}
                active={active}
                composing={composing}
              />
              <section className="motion-settings" aria-label="动画规格">
                <div className="section-heading">
                  {/* This is a sibling panel, including after the iframe's document heading. */}
                  <h2>画面与时间</h2>
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
                        min={1}
                        max={specificationMax}
                        step={1}
                        value={draft[key]}
                        aria-invalid={valid.field === key}
                        onChange={(event) => change(key, event.target.value)}
                      />
                    </Field>
                  ))}
                  <Field label="常用帧率" id="motion-fps">
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
                  <Field
                    label="自定义帧率（分子/分母）"
                    id="motion-custom-fps"
                    invalid={valid.field === "fps"}
                    hint="例如 24000/1001、120/1 或 1/2。"
                  >
                    <input
                      id="motion-custom-fps"
                      value={draft.fps}
                      spellCheck={false}
                      aria-invalid={valid.field === "fps"}
                      aria-describedby="motion-custom-fps-help"
                      onChange={(event) => change("fps", event.target.value)}
                    />
                  </Field>
                </div>
                <p className="panel-description">
                  支持自定义尺寸和帧率。预览使用低分辨率；正式导出受当前
                  GPU、内存、磁盘与编码器能力限制。
                </p>
              </section>
            </div>
          </div>
          <section className="motion-export" aria-label="动画视频导出">
            <div>
              <h2>动画导出</h2>
              <p className="panel-description">
                {draft.engine === "remotion-sdr-v1"
                  ? "SDR 网页源为 8-bit sRGB，可输出带完整色域与传递函数标签的 sRGB 视频。"
                  : "可选择保留 ACEScg EXR 母版，或仅输出 SDR sRGB / HDR PQ、HLG 视频。仅视频不生成 EXR 母版；预览显示独立选择。"}
              </p>
            </div>
            <Notice text={outputError} error />
            {/* Shared server discovery owns output choices; changing delivery never changes the preview. */}
            <div className="frame-settings">
              <Field
                label="正式导出采样"
                id="motion-render-scale"
                hint="2×2 / 4×4 提升空间边缘质量，渲染更慢；不添加运动模糊。快速预览不受影响。"
              >
                <select
                  id="motion-render-scale"
                  value={output.render_scale ?? 2}
                  disabled={!!pending || !outputOptions}
                  onChange={(event) => {
                    const renderScale = Number(event.currentTarget.value) as
                      1 | 2 | 4;
                    setOutput((old) => ({ ...old, render_scale: renderScale }));
                  }}
                >
                  {(outputOptions?.render_scales || [1, 2, 4]).map((scale) => (
                    <option key={scale} value={scale}>
                      {scale}×{scale}
                      {scale === 2
                        ? " · 推荐"
                        : scale === 1
                          ? " · 单次采样"
                          : " · 精细"}
                    </option>
                  ))}
                </select>
              </Field>
              {draft.engine === "three-float-v1" && (
                <Field
                  label="导出模式"
                  id="motion-delivery-mode"
                  hint={
                    output.master_bit_depth === null
                      ? "逐帧渲染并直接编码，不生成 EXR 母版，也不为视频再次渲染。中断后重新执行需从头渲染。"
                      : "保留 ACEScg EXR 母版；可同时输出视频。"
                  }
                >
                  <select
                    id="motion-delivery-mode"
                    value={
                      output.master_bit_depth === null ? "video-only" : "master"
                    }
                    disabled={!!pending || !outputOptions}
                    onChange={(event) => {
                      const onlyVideo =
                        event.currentTarget.value === "video-only";
                      // Preserve the selected video and last EXR precision when toggling modes.
                      const video =
                        output.video !== "none"
                          ? output.video
                          : outputOptions?.videos.find(
                              (item) => item.value !== "none" && item.available,
                            )?.value;
                      setOutput((old) => ({
                        ...old,
                        master_bit_depth: onlyVideo ? null : masterDepth,
                        video: onlyVideo ? video || "srgb" : old.video,
                      }));
                    }}
                  >
                    <option value="master">保留 EXR 母版</option>
                    <option
                      value="video-only"
                      disabled={
                        !outputOptions?.video_only ||
                        !outputOptions.videos.some(
                          (item) => item.value !== "none" && item.available,
                        )
                      }
                    >
                      仅视频
                    </option>
                  </select>
                </Field>
              )}
              {draft.engine === "three-float-v1" &&
                output.master_bit_depth !== null && (
                  <Field label="EXR 母版精度" id="motion-master-depth">
                    <select
                      id="motion-master-depth"
                      value={output.master_bit_depth ?? 32}
                      disabled={!!pending || !outputOptions}
                      onChange={(event) => {
                        // Capture before a discovery response rerenders the controlled select.
                        const depth = Number(event.currentTarget.value) as
                          16 | 32;
                        setMasterDepth(depth);
                        setOutput((old) => ({
                          ...old,
                          master_bit_depth: depth,
                        }));
                      }}
                    >
                      {(outputOptions?.master_bit_depths || [16, 32]).map(
                        (depth) => (
                          <option key={depth} value={depth}>
                            {depth}-bit {depth === 32 ? "Float" : "Half"} ·
                            ACEScg
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
                  {(outputOptions?.videos || [])
                    .filter(
                      (item) =>
                        output.master_bit_depth !== null ||
                        item.value !== "none",
                    )
                    .map((item) => (
                      <option
                        key={item.value}
                        value={item.value}
                        disabled={!item.available}
                      >
                        {item.label}
                        {!item.available
                          ? " · " + (item.reason || "本机缺少编码器")
                          : ""}
                      </option>
                    ))}
                </select>
              </Field>
            </div>
            <DeliveryDestination
              onPickingChange={setDirectoryPicking}
              variant="animation"
              videoOnly={
                draft.engine === "three-float-v1" &&
                output.master_bit_depth === null
              }
              options={destinationOptions}
              value={directory}
              error={destinationError}
              disabled={!!pending}
              onChange={(value) => {
                setDestination(value);
                setDestinationError("");
              }}
            />
            <MotionStorageEstimate
              spec={valid.spec}
              output={output}
              directory={directory}
              projectId={projectId}
              active={
                active &&
                !composing &&
                !directoryPicking &&
                outputOptions?.engine === draft.engine
              }
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
            {verification && (
              <p className="panel-description">
                {verification.float_pipeline_verified
                  ? "浮点链路已验证"
                  : "SDR 截图链路 · 不属于浮点母版"}
                {verification.source_color_contract?.status !==
                  "not_applicable" && (
                  <>
                    {" "}
                    ·{" "}
                    {verification.source_color_contract?.status === "reviewed"
                      ? "源码颜色契约已审核"
                      : "源码颜色契约未审核"}
                  </>
                )}
                {(verification.native_color_tags_verified ||
                  verification.video?.native_color_tags_verified) &&
                  " · 视频色彩标签已验证"}
              </p>
            )}
            {thumbnail && (
              <img
                className="preview-image"
                src={artifactUrl(thumbnail.id)}
                alt="导出画面的 sRGB 预览"
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
        open={!!reviewTarget}
        title="记录源码颜色契约审核"
        onClose={() => {
          if (!pending) setReviewTarget(null);
        }}
      >
        <p>
          仅记录修订 {reviewTarget?.revision_id.slice(0, 8)} 的审核。请确认最终
          RGB 使用各图层声明的线性空间（当前新内容为
          {spaceLabel(
            reviewTarget?.scene.source_color_space ?? "Linear Rec.709 (sRGB)",
          )}
          ），未烘焙显示 Gamma 或色调映射，且透明混合符合宿主 Alpha
          约定。此记录不会自动检查任意 shader，也不会阻止未审核代码渲染。
        </p>
        {/* Reuse the shared modal form padding and native keyboard submission. */}
        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            void recordReview();
          }}
        >
          <Field
            label="审核人"
            id="motion-reviewer"
            invalid={!!reviewError && !reviewer.trim()}
            hint={
              reviewError && !reviewer.trim() ? "请输入审核人。" : undefined
            }
          >
            <input
              id="motion-reviewer"
              autoFocus
              maxLength={120}
              value={reviewer}
              aria-invalid={!!reviewError && !reviewer.trim()}
              aria-describedby={
                reviewError && !reviewer.trim()
                  ? "motion-reviewer-help"
                  : undefined
              }
              disabled={!!pending}
              onChange={(event) => setReviewer(event.target.value)}
            />
          </Field>
          <Field
            label="审核依据"
            id="motion-review-notes"
            invalid={!!reviewError && !reviewNotes.trim()}
            hint={
              reviewError && !reviewNotes.trim()
                ? "请输入源码颜色契约的审核依据。"
                : undefined
            }
          >
            <Textarea
              className="resize-none"
              id="motion-review-notes"
              rows={5}
              maxLength={4000}
              value={reviewNotes}
              aria-invalid={!!reviewError && !reviewNotes.trim()}
              aria-describedby={
                reviewError && !reviewNotes.trim()
                  ? "motion-review-notes-help"
                  : undefined
              }
              disabled={!!pending}
              onChange={(event) => setReviewNotes(event.target.value)}
            />
          </Field>
          <Notice text={reviewError} error />
          <div className="modal-actions">
            <Button disabled={!!pending} onClick={() => setReviewTarget(null)}>
              取消
            </Button>
            <Button
              type="submit"
              className="primary"
              disabled={!!pending || composing}
            >
              保存审核记录
            </Button>
          </div>
        </form>
      </Modal>
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
