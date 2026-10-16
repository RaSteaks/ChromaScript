import { useEffect, useState } from "react";
import { call } from "./api";
import { Button, Notice } from "./components";
import type { MotionOutput, MotionSpec } from "./types";

type Estimate = {
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
}: {
  spec: MotionSpec | null;
  output: MotionOutput;
  directory: string;
  projectId: string;
  active: boolean;
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
  const data = state.key === key ? state.data : undefined;
  const error = state.key === key ? state.error : undefined;
  const floating = spec?.engine !== "remotion-sdr-v1";
  // Source precision does not imply that an EXR sequence is being delivered.
  const hasMaster = floating && output.master_bit_depth !== null;
  const videoOnly = floating && !hasMaster;
  return (
    <section className="motion-storage-estimate" aria-label="渲染磁盘空间预估">
      <div className="section-heading">
        <h3>{hasMaster ? "母版渲染预估磁盘空间" : "渲染预估磁盘空间"}</h3>
        <Button
          disabled={!active || !recipe}
          onClick={() => setRefresh((old) => old + 1)}
        >
          刷新预估
        </Button>
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
              <dt>导出盘所需空间预算</dt>
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
                  <div>
                    <dt>母版额外余量（10%）</dt>
                    <dd>{bytes(data.master_overhead_budget_bytes)}</dd>
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
        </>
      )}
      <p className="panel-description">
        {videoOnly
          ? "仅视频预算不含 EXR 母版；中间数据仅计一帧。视频按规划码率与时长估算并加余量，实际体积取决于画面，不是文件大小上限；运行时会检查剩余空间。"
          : "按当前草稿估算全新任务，不扣除已有缓存。ZIP 压缩与视频实际体积取决于画面；超采样增加中间数据，不改变最终尺寸。"}
        更新音轨或释放空间后可刷新预估。
      </p>
    </section>
  );
}
