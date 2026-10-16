import { useCallback, useEffect, useRef, useState } from "react";
import {
  Pause,
  Play,
  RefreshCw,
  SkipBack,
  StepBack,
  StepForward,
} from "lucide-react";
import { PreviewAudio, type PreviewAudioHandle } from "./PreviewAudio";
import { call } from "./api";
import { Button, Notice } from "./components";
import type { MotionSpec } from "./types";

type Picture = {
  html: string;
  channel: string;
  recipe: string;
  frames: number;
  ready: boolean;
};

/** A candidate proves it can mount before replacing the last working picture. */
export function MotionPreview({
  spec,
  sceneId,
  projectId,
  active,
  composing,
}: {
  spec: MotionSpec | null;
  sceneId: string;
  projectId: string;
  active: boolean;
  composing: boolean;
}) {
  const audio = useRef<PreviewAudioHandle>(null);
  const [pictures, setPictures] = useState<Picture[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<{
    message: string;
    channel?: string;
  } | null>(null);
  const [retry, setRetry] = useState(0);
  const [frame, setFrame] = useState(0);
  const [playing, setPlaying] = useState(false);
  const windows = useRef(new Map<string, HTMLIFrameElement>());
  const current = useRef(pictures);
  current.current = pictures;
  const failed = useRef(new Set<string>());
  const recipe = spec ? JSON.stringify(spec) : "";
  const accepted = pictures.find((picture) => picture.ready);
  const command = useCallback((type: string, value?: number) => {
    const picture = current.current.find((item) => item.ready);
    if (picture && type === "seek" && value !== undefined) {
      const spec: MotionSpec = JSON.parse(picture.recipe);
      audio.current?.seek((value * spec.fps.denominator) / spec.fps.numerator);
    }
    if (picture)
      windows.current
        .get(picture.channel)
        ?.contentWindow?.postMessage(
          { channel: picture.channel, type, value },
          "*",
        );
  }, []);

  useEffect(() => {
    setPictures([]);
    setFrame(0);
    setPlaying(false);
    setError(null);
    failed.current.clear();
  }, [sceneId]);

  useEffect(() => {
    // A replaced candidate must not let its old readiness timeout overwrite a
    // newer draft's status. Only committed pictures survive a recipe change.
    setPictures((old) => old.filter((item) => item.ready));
    if (!active || composing || !recipe) {
      setPending(false);
      return;
    }
    let cancelled = false;
    const abort = new AbortController();
    const timer = window.setTimeout(async () => {
      setPending(true);
      setError(null);
      command("pause");
      try {
        const reply = await call<{ html: string; channel: string }>(
          "compile_motion_preview",
          { spec: JSON.parse(recipe) },
          AbortSignal.any([abort.signal, AbortSignal.timeout(50000)]),
        );
        if (!cancelled)
          setPictures((old) => [
            ...old.filter((item) => item.ready),
            {
              ...reply,
              recipe,
              frames: JSON.parse(recipe).frames,
              ready: false,
            },
          ]);
      } catch (cause) {
        if (!cancelled) {
          setError({
            message: cause instanceof Error ? cause.message : String(cause),
          });
          setPending(false);
        }
      }
    }, 350);
    return () => {
      cancelled = true;
      abort.abort();
      window.clearTimeout(timer);
    };
  }, [recipe, active, composing, sceneId, retry, command]);

  useEffect(() => {
    const candidate = pictures.find((picture) => !picture.ready);
    if (!candidate) return;
    const timer = window.setTimeout(() => {
      failed.current.add(candidate.channel);
      setPictures((old) =>
        old.filter((picture) => picture.channel !== candidate.channel),
      );
      setPending(false);
      setError({
        message: "预览启动超时，已保留上一版画面。请检查代码后重新编译。",
      });
    }, 15000);
    return () => window.clearTimeout(timer);
  }, [pictures]);

  useEffect(() => {
    const message = (event: MessageEvent) => {
      const data = event.data;
      if (!data || typeof data.channel !== "string") return;
      const picture = current.current.find(
        (item) => item.channel === data.channel,
      );
      if (
        !picture ||
        windows.current.get(data.channel)?.contentWindow !== event.source ||
        failed.current.has(data.channel)
      )
        return;
      if (data.type === "error") {
        // Reject broken candidates, but keep a mounted renderer's channel alive:
        // a later seek can recover from a frame-specific GPU/runtime failure.
        if (!picture.ready) failed.current.add(data.channel);
        setError({
          message: String(data.value),
          channel: picture.ready ? data.channel : undefined,
        });
        setPending(false);
        setPlaying(false);
        if (!picture.ready)
          setPictures((old) =>
            old.filter((item) => item.channel !== data.channel),
          );
      }
      if (data.type === "ready" && !picture.ready) {
        // Ignore results for a draft replaced while the iframe was mounting.
        if (picture.recipe !== recipe) return;
        setPictures((old) =>
          old
            .filter((item) => item.channel === data.channel)
            .map((item) => ({ ...item, ready: true })),
        );
        setPending(false);
        setError(null);
        setPlaying(false);
        windows.current.get(data.channel)?.contentWindow?.postMessage(
          {
            channel: data.channel,
            type: "seek",
            value: Math.min(frame, picture.frames - 1),
          },
          "*",
        );
      }
      if (
        picture.ready &&
        data.type === "frame" &&
        Number.isInteger(data.value) &&
        data.value >= 0 &&
        data.value < picture.frames
      ) {
        setFrame(data.value);
        // Only an acknowledged rendered frame clears this picture's error;
        // playback of a retained picture must not hide a new draft's failure.
        setError((old) => (old?.channel === data.channel ? null : old));
      }
      if (
        picture.ready &&
        data.type === "playing" &&
        typeof data.value === "boolean"
      )
        setPlaying(data.value);
    };
    window.addEventListener("message", message);
    return () => window.removeEventListener("message", message);
  }, [recipe, frame]);

  useEffect(() => {
    const pauseHidden = () => {
      if (!active || document.hidden) command("pause");
    };
    pauseHidden();
    document.addEventListener("visibilitychange", pauseHidden);
    return () => document.removeEventListener("visibilitychange", pauseHidden);
  }, [active, command]);

  const stale = !!accepted && accepted.recipe !== recipe;
  // Retained pictures keep their own rational rate when a replacement draft fails.
  const acceptedSpec: MotionSpec | null = accepted
    ? JSON.parse(accepted.recipe)
    : null;
  const rate = acceptedSpec
    ? acceptedSpec.fps.numerator / acceptedSpec.fps.denominator
    : 1;
  const maxFrame = (accepted?.frames || spec?.frames || 1) - 1;
  return (
    <section className="motion-preview-panel" aria-label="动画实时预览">
      <div className="section-heading">
        <h2>实时预览</h2>
        <span className="tiny-label">
          {spec?.engine === "remotion-sdr-v1"
            ? "Remotion · SDR"
            : "ACES · sRGB"}
        </span>
      </div>
      <div className="motion-preview-surface" aria-busy={pending}>
        {pictures.map((picture) => (
          <iframe
            key={picture.channel}
            title={picture.ready ? "动画画面" : "正在准备的动画画面"}
            className={
              picture.ready ? "motion-frame" : "motion-frame candidate"
            }
            sandbox="allow-scripts"
            ref={(node) => {
              if (node) windows.current.set(picture.channel, node);
              else windows.current.delete(picture.channel);
            }}
            srcDoc={picture.html}
            tabIndex={-1}
            aria-hidden={!picture.ready}
          />
        ))}
        {!accepted && (
          <div className="empty-state">
            <Play size={30} aria-hidden="true" />
            <p>
              {pending ? "正在准备动画画面…" : "编辑代码后，画面将在这里显示。"}
            </p>
          </div>
        )}
      </div>
      <div className="motion-preview-status" role="status">
        {pending
          ? "正在编译草稿…"
          : error
            ? error.channel
              ? "当前帧未能预览；可拖动时间线重试，或重新编译。"
              : accepted
                ? "当前草稿未能预览；上一版可用画面已保留。"
                : "当前草稿未能预览，请检查下方错误。"
            : stale
              ? "显示上一版画面，等待当前草稿编译。"
              : accepted
                ? "当前草稿 · 网页 sRGB 预览"
                : "等待代码预览"}
      </div>
      <div className="motion-playback">
        <Button
          onClick={() => {
            if (!playing) audio.current?.unlock();
            command(playing ? "pause" : "play");
          }}
          disabled={!accepted}
          aria-label={playing ? "暂停动画" : "播放动画"}
        >
          {playing ? <Pause size={15} /> : <Play size={15} />}
          {playing ? "暂停" : "播放"}
        </Button>
        <Button
          onClick={() => command("seek", 0)}
          disabled={!accepted}
          aria-label="回到动画首帧"
        >
          <SkipBack size={15} />
        </Button>
        <Button
          onClick={() => command("seek", Math.max(0, frame - 1))}
          disabled={!accepted}
          aria-label="上一帧动画"
        >
          <StepBack size={15} />
        </Button>
        <Button
          onClick={() => command("seek", Math.min(maxFrame, frame + 1))}
          disabled={!accepted}
          aria-label="下一帧动画"
        >
          <StepForward size={15} />
        </Button>
        <span className="mono">
          {frame} / {maxFrame}
        </span>
        <Button
          onClick={() => setRetry((old) => old + 1)}
          disabled={pending || !spec}
          aria-label="重新编译动画"
        >
          <RefreshCw size={15} />
        </Button>
      </div>
      <label className="sr-only" htmlFor="motion-timeline">
        动画时间线
      </label>
      <input
        id="motion-timeline"
        type="range"
        min={0}
        max={maxFrame}
        value={Math.min(frame, maxFrame)}
        disabled={!accepted}
        onChange={(event) => {
          command("pause");
          command("seek", Number(event.target.value));
        }}
      />
      <PreviewAudio
        key={`${projectId}:${sceneId}`}
        ref={audio}
        projectId={projectId}
        active={active}
        playing={playing && !!accepted && !error?.channel}
        frameDuration={1 / rate}
        seconds={frame / rate}
        duration={(maxFrame + 1) / rate}
        clockKey={`${sceneId}:${accepted?.channel}`}
      />
      <Notice text={error?.message || ""} error />
      <p className="technical-note">
        {spec?.engine === "remotion-sdr-v1"
          ? "SDR 网页画面 · sRGB 8-bit。保留网页图层与原始动画代码。"
          : "GPU 浮点场景 · ACES sRGB 预览。独立保存 ACEScg 母版，支持 HDR 和 SDR 交付。"}
      </p>
    </section>
  );
}
