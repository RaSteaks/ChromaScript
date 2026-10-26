import { useEffect, useRef, useState } from "react";
import { call } from "./api";
import { Button, Notice } from "./components";
import type { MotionOutput, MotionSpec } from "./types";

type Estimate = {
  estimated_output_bytes: number;
  compressed_master_bytes: number | null;
  unmeasured_components: string[];
  master_frame_bytes: number;
  master_uncompressed_bytes: number;
  master_overhead_budget_bytes: number;
  intermediate_budget_bytes: number;
  video_budget_bytes: number;
  audio_bytes: number;
  auxiliary_budget_bytes: number;
  destination_required_bytes: number;
  workspace_required_bytes: number;
  destination_free_bytes: number;
  workspace_free_bytes: number;
  same_volume: boolean;
  destination_sufficient: boolean;
  workspace_sufficient: boolean;
  sufficient: boolean;
};
export type StorageSample = { key: string; id: string };
type Sample = {
  id: string;
  status: "running" | "completed" | "failed" | "cancelled" | "interrupted";
  completed_frames: number;
  sample_count: number;
  budget?: Estimate;
  error?: string;
};

/** Source content affects compression; names do not. This same identity guards
 * submission so an effect cannot briefly submit a previously sampled draft. */
export function motionSampleKey(
  spec: MotionSpec | null,
  output: MotionOutput,
  directory: string,
  projectId: string,
) {
  if (!spec) return "";
  const content = { ...spec, name: "" };
  return JSON.stringify({ content, output, directory, projectId });
}
const bytes = (value: number) => {
  const unit = value >= 1024 ** 4 ? "TiB" : value >= 1024 ** 3 ? "GiB" : "MiB";
  const divisor = 1024 ** (unit === "TiB" ? 4 : unit === "GiB" ? 3 : 2);
  return (
    new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(
      value / divisor,
    ) +
    " " +
    unit
  );
};

/** Display estimates for the current draft only; stale disk/recipe replies cannot
 * overwrite newer settings. Submission always performs its own fresh check. */
export function MotionStorageEstimate({
  spec,
  output,
  directory,
  projectId,
  active,
  samplingActive,
  onSample,
}: {
  spec: MotionSpec | null;
  output: MotionOutput;
  directory: string;
  projectId: string;
  active: boolean;
  samplingActive: boolean;
  onSample: (sample: StorageSample | null) => void;
}) {
  const [refresh, setRefresh] = useState(0);
  const [state, setState] = useState<{
    key: string;
    data?: Estimate;
    error?: string;
  }>({ key: "" });
  // Code/name edits do not change storage. Key only the numeric recipe and volume.
  const recipe =
    spec && directory.trim()
      ? JSON.stringify({
          settings: {
            width: spec.width,
            height: spec.height,
            frames: spec.frames,
            fps: spec.fps,
          },
          engine: spec.engine,
          output,
          export_directory: directory,
          project_id: projectId,
        })
      : "";
  const key = recipe + ":" + refresh;
  const sampleKey = motionSampleKey(spec, output, directory, projectId);
  const current = useRef({ sampleKey, active: samplingActive });
  current.current = { sampleKey, active: samplingActive };
  const request = useRef<{ key: string; id: string } | null>(null);
  const [sampling, setSampling] = useState(false);
  const [sampleError, setSampleError] = useState("");
  const [sample, setSample] = useState<{ key: string; value: Sample } | null>(
    null,
  );
  const selectedSample = sample?.key === sampleKey ? sample.value : undefined;
  const canSample =
    spec?.engine === "three-float-v1" &&
    (output.master_bit_depth !== null || output.video.startsWith("prores-"));

  useEffect(() => {
    onSample(null);
  }, [sampleKey, onSample]);

  async function startSample() {
    if (!spec || !active || !canSample) return;
    const captured = sampleKey;
    if (request.current?.key !== captured)
      request.current = { key: captured, id: crypto.randomUUID() };
    setSampling(true);
    setSampleError("");
    try {
      const reply = await call<{ estimate: Sample }>(
        "start_motion_render_estimate",
        {
          project_id: projectId,
          spec,
          output,
          export_directory: directory,
          idempotency_key: request.current.id,
        },
      );
      request.current = null;
      // A late start response still owns a server task; explicitly cancel it.
      if (current.current.sampleKey !== captured || !current.current.active) {
        await call("cancel_motion_render_estimate", {
          estimate_id: reply.estimate.id,
        });
        return;
      }
      setSample({ key: captured, value: reply.estimate });
    } catch (cause) {
      if (current.current.sampleKey === captured)
        setSampleError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSampling(false);
    }
  }

  useEffect(() => {
    const id = selectedSample?.id;
    if (!id || !samplingActive) return;
    let stale = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function poll() {
      try {
        const reply = await call<{ estimate: Sample }>(
          "get_motion_render_estimate",
          { estimate_id: id },
          controller.signal,
        );
        if (stale) return;
        setSample({ key: sampleKey, value: reply.estimate });
        setSampleError("");
        if (reply.estimate.status === "completed")
          onSample({ key: sampleKey, id: reply.estimate.id });
        else if (reply.estimate.status === "running")
          timer = setTimeout(() => void poll(), 500);
      } catch (cause) {
        if (!stale)
          setSampleError(
            cause instanceof Error ? cause.message : String(cause),
          );
      }
    }
    void poll();
    return () => {
      stale = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [selectedSample?.id, samplingActive, sampleKey, refresh, onSample]);

  useEffect(() => {
    const id = selectedSample?.id;
    return () => {
      // Ownership survives temporary capability refreshes and name-only saves.
      // Only recipe/view changes release this sampling task.
      if (id)
        void call("cancel_motion_render_estimate", { estimate_id: id }).catch(
          () => {},
        );
    };
  }, [selectedSample?.id, sampleKey, samplingActive]);
  useEffect(
    () => () => {
      current.current.active = false;
    },
    [],
  );
  useEffect(() => {
    if (!active || !recipe) return;
    const controller = new AbortController();
    let stale = false;
    const timer = setTimeout(() => {
      const timeout = setTimeout(() => controller.abort(), 10000);
      void call<Estimate>(
        "estimate_motion_render",
        JSON.parse(recipe),
        controller.signal,
      )
        .then((data) => {
          if (!stale) setState({ key, data });
        })
        .catch((error: unknown) => {
          if (!stale)
            setState({
              key,
              error: controller.signal.aborted
                ? "磁盘预估超时，请刷新重试。"
                : error instanceof Error
                  ? error.message
                  : String(error),
            });
        })
        .finally(() => clearTimeout(timeout));
    }, 300);
    return () => {
      stale = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [active, recipe, key]);
  const data =
    selectedSample?.status === "completed"
      ? selectedSample.budget
      : state.key === key
        ? state.data
        : undefined;
  const error = state.key === key ? state.error : undefined;
  const floating = spec?.engine !== "remotion-sdr-v1";
  // Source precision does not imply that an EXR sequence is being delivered.
  const hasMaster = floating && output.master_bit_depth !== null;
  const videoOnly = floating && !hasMaster;
  return (
    <section className="motion-storage-estimate" aria-label="渲染磁盘空间预估">
      <div className="section-heading">
        <h3>{hasMaster ? "母版渲染预估磁盘空间" : "渲染预估磁盘空间"}</h3>
        <div>
          <Button
            disabled={!active || !recipe}
            onClick={() => setRefresh((old) => old + 1)}
          >
            刷新预估
          </Button>
          {canSample && (
            <Button
              disabled={
                !active ||
                !recipe ||
                sampling ||
                selectedSample?.status === "running"
              }
              onClick={() => void startSample()}
            >
              {sampling ? "正在启动采样…" : "采样估算"}
            </Button>
          )}
          {selectedSample?.status === "running" && (
            <Button
              onClick={() => {
                void call("cancel_motion_render_estimate", {
                  estimate_id: selectedSample.id,
                }).catch((cause: unknown) =>
                  setSampleError(
                    cause instanceof Error ? cause.message : String(cause),
                  ),
                );
              }}
            >
              取消采样
            </Button>
          )}
        </div>
      </div>
      <div className="status-line" aria-live="polite">
        {!recipe
          ? "填写有效的画面规格与导出位置后显示预估。"
          : error
            ? "预估未完成"
            : !data
              ? "正在预估磁盘空间…"
              : data.sufficient
                ? "当前磁盘空间满足预算；提交时会重新检查。"
                : "当前磁盘空间不足，请更换目录或降低渲染规格。"}
      </div>
      <Notice text={error || ""} error />
      <div className="status-line" aria-live="polite">
        {selectedSample?.status === "running"
          ? `正在采样 ${selectedSample.completed_frames} / ${selectedSample.sample_count} 帧…`
          : selectedSample?.status === "completed"
            ? `基于 ${selectedSample.sample_count} 帧的采样平均值，提交时采用此估算。`
            : selectedSample?.status === "cancelled"
              ? "采样已取消，继续显示规格估算。"
              : sample && sample.key !== sampleKey
                ? "画面或输出已改变，旧采样已失效。"
                : "规格估算；采样可测量画面压缩率，不增加额外余量。"}
      </div>
      <Notice text={sampleError || selectedSample?.error || ""} error />
      {data && (
        <>
          <dl className="specification-summary">
            <div>
              <dt>{hasMaster ? "EXR 母版（未压缩）" : "EXR 母版"}</dt>
              <dd>
                {hasMaster
                  ? bytes(data.master_uncompressed_bytes)
                  : videoOnly
                    ? "不生成（仅视频）"
                    : "SDR 源不生成浮点母版"}
              </dd>
            </div>
            <div>
              <dt>预计成品体积</dt>
              <dd>{bytes(data.estimated_output_bytes)}</dd>
            </div>
            {data.compressed_master_bytes !== null && (
              <div>
                <dt>EXR 压缩后估算</dt>
                <dd>{bytes(data.compressed_master_bytes)}</dd>
              </div>
            )}
            <div>
              <dt>预计磁盘峰值占用</dt>
              <dd>{bytes(data.destination_required_bytes)}</dd>
            </div>
            <div>
              <dt>导出盘可用空间</dt>
              <dd>{bytes(data.destination_free_bytes)}</dd>
            </div>
            {!data.same_volume && (
              <div>
                <dt>工作区所需 / 可用</dt>
                <dd>
                  {bytes(data.workspace_required_bytes)} /{" "}
                  {bytes(data.workspace_free_bytes)}
                </dd>
              </div>
            )}
          </dl>
          {!data.sufficient && (
            <Notice
              error
              text={
                !data.destination_sufficient
                  ? "导出盘空间不足。预估不预留空间，请更换目录或释放空间后刷新。"
                  : "本机工作区空间不足，请释放空间后刷新。"
              }
            />
          )}
          <details className="technical-note">
            <summary>查看空间预算明细</summary>
            <dl className="specification-summary">
              {hasMaster && (
                <>
                  <div>
                    <dt>单帧 RGBA 母版</dt>
                    <dd>{bytes(data.master_frame_bytes)}</dd>
                  </div>
                </>
              )}
              <div>
                <dt>中间数据预算</dt>
                <dd>{bytes(data.intermediate_budget_bytes)}</dd>
              </div>
              <div>
                <dt>视频预算</dt>
                <dd>{bytes(data.video_budget_bytes)}</dd>
              </div>
              <div>
                <dt>音轨 WAV</dt>
                <dd>{bytes(data.audio_bytes)}</dd>
              </div>
              <div>
                <dt>辅助文件预算</dt>
                <dd>{bytes(data.auxiliary_budget_bytes)}</dd>
              </div>
              <div>
                <dt>
                  工作区元数据预算
                  {data.same_volume ? "（已计入同一磁盘）" : "（单独磁盘）"}
                </dt>
                <dd>{bytes(data.workspace_required_bytes)}</dd>
              </div>
            </dl>
          </details>
          {data.unmeasured_components?.map((note) => (
            <p className="panel-description" key={note}>
              {note}
            </p>
          ))}
        </>
      )}
      <p className="panel-description">
        {videoOnly
          ? "仅视频不生成 EXR，临时浮点数据每路最多保留一帧。不加百分比余量；估算不是文件大小上限，运行时检查实际剩余空间。"
          : "按当前草稿估算全新任务，不加百分比余量；采样使用实际文件平均大小。超采样增加临时数据，不改变最终尺寸。"}
        更新音轨或释放空间后可刷新预估。
      </p>
    </section>
  );
}
