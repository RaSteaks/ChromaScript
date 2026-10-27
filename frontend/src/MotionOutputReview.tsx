import { useEffect, useMemo, useRef, useState } from "react";
import { call } from "./api";
import { Button, Field, Notice, PreviewPanel } from "./components";
import { PreviewTimeline } from "./PreviewTimeline";
import type { MotionOutput, MotionSpec } from "./types";

type TimePlan = {
  frames: number;
  fps: MotionSpec["fps"];
  source_frames: number;
  source_fps: MotionSpec["fps"];
  temporal_samples: number;
  shutter_angle: number;
};
type PlanReply = {
  plan: TimePlan;
  source_duration: number;
  output_duration: number;
};
type ReviewReply = {
  images: { frame: number; data: string; mime_type: string }[];
  time_plan: TimePlan;
};

/** Output-only drafts never modify the saved source composition or its player. */
export function MotionOutputReview({
  spec,
  output,
  active,
  onChange,
  onReady,
}: {
  spec: MotionSpec | null;
  output: MotionOutput;
  active: boolean;
  onChange: (output: MotionOutput) => void;
  onReady: (ready: boolean) => void;
}) {
  const [rateText, setRateText] = useState(
    output.fps ? `${output.fps.numerator}/${output.fps.denominator}` : "",
  );
  const [plan, setPlan] = useState<(PlanReply & { key: string }) | null>(null);
  const [planError, setPlanError] = useState("");
  const [retry, setRetry] = useState(0);
  const [startSeconds, setStartSeconds] = useState("0");
  const [count, setCount] = useState(30);
  const [review, setReview] = useState<(ReviewReply & { key: string }) | null>(
    null,
  );
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [playing, setPlaying] = useState(false);
  const [index, setIndex] = useState(0);
  const request = useRef<AbortController | null>(null);
  const decodedImages = useRef<HTMLImageElement[]>([]);
  const rateParts = rateText.split("/").map(Number);
  const validRate =
    rateText === "" ||
    (/^\d+\/\d+$/.test(rateText) &&
      rateParts.every(
        (value) =>
          Number.isSafeInteger(value) && value >= 1 && value <= 2147483647,
      ));
  const planKey =
    spec && validRate
      ? JSON.stringify({
          settings: {
            width: spec.width,
            height: spec.height,
            frames: spec.frames,
            fps: spec.fps,
          },
          output: {
            fps: output.fps ?? null,
            temporal_samples: output.temporal_samples ?? 1,
            shutter_angle: output.shutter_angle ?? 180,
          },
        })
      : "";
  const ready = !!planKey && plan?.key === planKey;
  // Source graphs can be large; playback changes only the image index.
  const recipe = useMemo(
    () => JSON.stringify({ spec, output, rateText }),
    [spec, output, rateText],
  );
  const currentIndex = useRef(index);
  currentIndex.current = index;
  const current = useRef(recipe);
  current.current = recipe;
  const stale = review?.key !== recipe;
  const validStart =
    Number.isFinite(Number(startSeconds)) &&
    Number(startSeconds) >= 0 &&
    startSeconds.trim() !== "";

  useEffect(() => {
    setRateText(
      output.fps ? `${output.fps.numerator}/${output.fps.denominator}` : "",
    );
  }, [output.fps]);
  useEffect(() => {
    onReady(ready);
  }, [ready, onReady, output, rateText]);
  useEffect(() => {
    if (!active || !planKey) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setPlanError("");
      void call<PlanReply>(
        "get_motion_time_plan",
        JSON.parse(planKey),
        controller.signal,
      )
        .then((reply) => {
          if (!controller.signal.aborted) setPlan({ ...reply, key: planKey });
        })
        .catch((cause: Error) => {
          if (!controller.signal.aborted) setPlanError(cause.message);
        });
    }, 180);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [active, planKey, retry]);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    if (!active || stale) setPlaying(false);
  }, [active, stale]);
  useEffect(() => {
    const pause = () => {
      if (document.hidden) setPlaying(false);
    };
    document.addEventListener("visibilitychange", pause);
    return () => document.removeEventListener("visibilitychange", pause);
  }, []);
  useEffect(() => {
    if (!playing || !review || stale || !active) return;
    const start = performance.now();
    const first = currentIndex.current;
    const fps =
      review.time_plan.fps.numerator / review.time_plan.fps.denominator;
    let timer = 0;
    const tick = () => {
      const next =
        first + Math.floor(((performance.now() - start) * fps) / 1000);
      if (next >= review.images.length) {
        setIndex(review.images.length - 1);
        setPlaying(false);
        return;
      }
      setIndex(next);
      timer = requestAnimationFrame(tick);
    };
    timer = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(timer);
    // Seeking stops playback; subsequent image acknowledgements must not restart its clock.
  }, [playing, review, stale, active]);

  function changeRate(value: string) {
    setRateText(value);
    onReady(false);
    const parts = value.split("/").map(Number);
    if (value === "") onChange({ ...output, fps: null });
    else if (
      /^\d+\/\d+$/.test(value) &&
      parts.every((n) => Number.isSafeInteger(n) && n >= 1 && n <= 2147483647)
    )
      onChange({
        ...output,
        fps: { numerator: parts[0], denominator: parts[1] },
      });
  }
  async function generate() {
    if (!spec || !ready || !validStart || !plan || pending) return;
    setPending(true);
    setError("");
    setPlaying(false);
    const captured = recipe;
    const controller = new AbortController();
    request.current = controller;
    const frame = Math.min(
      plan.plan.frames - 1,
      // Seek to the closest output frame (e.g. 1.001s at 30000/1001 is frame 30).
      Math.round(
        (Number(startSeconds) * plan.plan.fps.numerator) /
          plan.plan.fps.denominator,
      ),
    );
    try {
      const result = await call<ReviewReply>(
        "render_motion_output_preview",
        {
          spec,
          output,
          start_frame: frame,
          frame_count: count,
        },
        controller.signal,
      );
      if (!controller.signal.aborted && current.current === captured) {
        // Decode the bounded clip before enabling playback, so PNG decode work
        // cannot present a previous image under a newer output-frame label.
        const decoded = await Promise.all(
          result.images.map(async (frame) => {
            const image = new Image();
            image.src = `data:${frame.mime_type};base64,${frame.data}`;
            await image.decode();
            return image;
          }),
        );
        if (!controller.signal.aborted && current.current === captured) {
          decodedImages.current = decoded;
          setReview({ ...result, key: captured });
          setIndex(0);
        }
      }
    } catch (cause) {
      if (!controller.signal.aborted && current.current === captured)
        setError((cause as Error).message);
    } finally {
      if (!controller.signal.aborted) setPending(false);
    }
  }
  const presets = [
    "",
    "24/1",
    "25/1",
    "30/1",
    "50/1",
    "60/1",
    "24000/1001",
    "30000/1001",
    "60000/1001",
  ];
  const image = review?.images[index];
  return (
    <section aria-label="输出时间与预览">
      <div className="motion-export-layout">
        <section className="motion-export-settings" aria-label="输出时间设置">
          <div className="section-heading">
            <h3>输出时间</h3>
          </div>
          <div className="frame-settings">
            <Field
              label="输出帧率"
              id="motion-output-fps"
              hint="仅改变本次交付；保留项目时长与动画节奏。"
            >
              <select
                id="motion-output-fps"
                value={presets.includes(rateText) ? rateText : "custom"}
                onChange={(event) => {
                  if (event.target.value !== "custom")
                    changeRate(event.target.value);
                }}
              >
                {presets.map((rate) => (
                  <option key={rate} value={rate}>
                    {rate ? `${rate} fps` : "跟随项目"}
                  </option>
                ))}
                {!presets.includes(rateText) && (
                  <option value="custom">自定义</option>
                )}
              </select>
            </Field>
            <Field
              label="自定义输出帧率"
              id="motion-output-custom-fps"
              invalid={!validRate}
              hint="分子/分母，例如 30000/1001；留空跟随项目。"
            >
              <input
                id="motion-output-custom-fps"
                value={rateText}
                aria-invalid={!validRate}
                aria-describedby="motion-output-custom-fps-help"
                onChange={(event) => changeRate(event.target.value)}
              />
            </Field>
            <Field
              label="时间采样"
              id="motion-temporal-samples"
              hint="多重采样减轻快速运动的顿挫，增加渲染时间；源码须支持小数帧与绝对时间求值。"
            >
              <select
                id="motion-temporal-samples"
                value={output.temporal_samples ?? 1}
                onChange={(event) => {
                  onReady(false);
                  onChange({
                    ...output,
                    temporal_samples: Number(event.target.value) as
                      1 | 4 | 8 | 16,
                  });
                }}
              >
                <option value={1}>快速抽帧</option>
                <option value={4}>时间多重采样 · 4 次</option>
                <option value={8}>时间多重采样 · 8 次</option>
                <option value={16}>时间多重采样 · 16 次</option>
              </select>
            </Field>
            <Field
              label="快门角"
              id="motion-shutter-angle"
              hint="曝光以输出帧时间为中心；180° 是半帧曝光。"
            >
              <select
                id="motion-shutter-angle"
                disabled={(output.temporal_samples ?? 1) === 1}
                value={output.shutter_angle ?? 180}
                onChange={(event) => {
                  onReady(false);
                  onChange({
                    ...output,
                    shutter_angle: Number(event.target.value),
                  });
                }}
              >
                {[90, 180, 270, 360].map((angle) => (
                  <option key={angle} value={angle}>
                    {angle}°
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Notice
            text={!validRate ? "输出帧率须为正整数分子/分母。" : planError}
            error
          />
          {planError && (
            <Button onClick={() => setRetry((value) => value + 1)}>
              重试时间计算
            </Button>
          )}
          <p className="panel-description" role="status">
            {ready && plan
              ? `项目 ${plan.plan.source_frames} 帧 · ${plan.source_duration.toFixed(4)} 秒 → 输出 ${plan.plan.frames} 帧 · ${plan.plan.fps.numerator}/${plan.plan.fps.denominator} fps · ${plan.output_duration.toFixed(4)} 秒`
              : !validRate
                ? "请修正输出帧率。"
                : planError
                  ? "输出时间未确认。"
                  : "正在确认输出时间设置…"}
          </p>
          {ready && plan && plan.output_duration > plan.source_duration && (
            <p className="panel-description">
              输出帧数向上取整，尾部保持不足一帧；不拉伸动画。
            </p>
          )}
        </section>
        <PreviewPanel
          label="输出采样预览"
          className="motion-preview-panel"
          active={active}
          toolbar={
            <div className="section-heading">
              <h3>输出采样预览</h3>
              <span className="tiny-label">sRGB · 编码前</span>
            </div>
          }
        >
          <div className="motion-preview-surface" aria-busy={pending}>
            {image ? (
              <img
                className="motion-frame"
                style={{ objectFit: "contain" }}
                src={`data:${image.mime_type};base64,${image.data}`}
                alt={`输出第 ${image.frame} 帧`}
              />
            ) : (
              <div className="empty-state">
                生成一段短预览，检查实际输出的运动节奏。
              </div>
            )}
          </div>
          <div className="frame-settings">
            <Field
              label="预览起点（秒）"
              id="motion-output-start"
              invalid={!validStart}
              hint="非负秒数；定位到最近的输出帧，超出片尾时查看末帧。"
            >
              <input
                id="motion-output-start"
                type="number"
                min={0}
                step="any"
                value={startSeconds}
                aria-invalid={!validStart}
                aria-describedby="motion-output-start-help"
                onChange={(event) => setStartSeconds(event.target.value)}
              />
            </Field>
            <Field label="预览帧数" id="motion-output-count">
              <select
                id="motion-output-count"
                value={count}
                onChange={(event) => setCount(Number(event.target.value))}
              >
                {[1, 15, 30, 60].map((value) => (
                  <option key={value} value={value}>
                    {value} 帧
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Button
            disabled={!ready || !validStart || pending}
            onClick={() => void generate()}
          >
            {pending ? "正在生成输出预览…" : "生成输出预览"}
          </Button>
          <PreviewTimeline
            frame={index}
            maxFrame={(review?.images.length ?? 1) - 1}
            playing={playing}
            disabled={!image || stale || pending}
            onPlay={() => {
              if (!playing && index === (review?.images.length ?? 0) - 1)
                setIndex(0);
              setPlaying(!playing);
            }}
            onSeek={(value) => {
              setPlaying(false);
              setIndex(value);
            }}
          />
          <Notice text={error} error />
          <p className="panel-description" role="status">
            {review && stale
              ? "设置或源码已改变，当前画面为旧预览；请重新生成。"
              : image && review
                ? `输出第 ${image.frame} 帧 · ${((image.frame * review.time_plan.fps.denominator) / review.time_plan.fps.numerator).toFixed(4)} 秒`
                : "预览不会保存源码或创建导出任务。"}
          </p>
          <p className="panel-description">
            按正式空间与时间采样渲染，再缩小为 sRGB 查看图。HDR
            交付的显示效果和有损编码误差不包含在此预览中；短片段播放结束后停止。
          </p>
        </PreviewPanel>
      </div>
    </section>
  );
}
