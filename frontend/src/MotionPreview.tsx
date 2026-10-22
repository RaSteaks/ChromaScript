import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Play, RefreshCw } from "lucide-react";
import { PreviewTimeline } from "./PreviewTimeline";
import { PreviewAudio, type PreviewAudioHandle } from "./PreviewAudio";
import { call, encodeHDRPreview } from "./api";
import {
  Button,
  Field,
  Notice,
  PreviewPanel,
  type PreviewPanelHandle,
} from "./components";
import type { MotionSpec } from "./types";
import {
  createHDRPreview,
  hdrVideoSupport,
  hdrVideoCapabilities,
  type HDRController,
} from "../motion/hdr-preview.mjs";

type Picture = {
  html: string;
  channel: string;
  recipe: string;
  frames: number;
  ready: boolean;
  hdrPreview: boolean;
  sceneId: string;
  retry: number;
  hdr?: { mode: "hdr" | "sdr" | "buffering"; message: string };
};

/** A candidate proves it can mount before replacing the last working picture. */
export const MotionPreview = memo(function MotionPreview({
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
  const [hdrPreview, setHdrPreview] = useState(true);
  const windows = useRef(new Map<string, HTMLIFrameElement>());
  const panel = useRef<PreviewPanelHandle>(null);
  const current = useRef(pictures);
  current.current = pictures;
  const failed = useRef(new Set<string>());
  const videoHost = useRef<HTMLDivElement>(null);
  const hdr = useRef<HDRController | null>(null);
  const captureSequence = useRef(0);
  const latestFrame = useRef(frame);
  latestFrame.current = frame;
  // A frame acknowledgement must not serialize up to 2 MB of unchanged source.
  const recipe = useMemo(() => (spec ? JSON.stringify(spec) : ""), [spec]);
  const accepted = pictures.find((picture) => picture.ready);
  const command = useCallback((type: string, value?: number) => {
    const picture = current.current.find((item) => item.ready);
    if (picture && type === "seek" && value !== undefined) {
      const spec: MotionSpec = JSON.parse(picture.recipe);
      audio.current?.seek((value * spec.fps.denominator) / spec.fps.numerator);
    }
    if (hdr.current?.enabled) {
      if (type === "play") {
        hdr.current.play();
        return;
      }
      if (type === "pause") hdr.current.pause();
      if (type === "seek" && value !== undefined) hdr.current.seek(value);
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

  const acceptedChannel = accepted?.channel;
  const acceptedRecipe = accepted?.recipe;
  const acceptedHDR = accepted?.hdrPreview;
  useEffect(() => {
    if (!acceptedChannel || !acceptedRecipe || !videoHost.current) return;
    const source: MotionSpec = JSON.parse(acceptedRecipe);
    if (
      !acceptedHDR ||
      source.engine !== "three-float-v1" ||
      !source.source_color_space ||
      source.source_color_space === "Linear Rec.709 (sRGB)"
    )
      return;
    const status = (value: NonNullable<Picture["hdr"]>) =>
      setPictures((old) =>
        old.map((item) =>
          item.channel === acceptedChannel ? { ...item, hdr: value } : item,
        ),
      );
    const scale = Math.min(
      1,
      1280 / Math.max(source.width, source.height),
      720 / Math.min(source.width, source.height),
    );
    const width = Math.floor((source.width * scale) / 2) * 2;
    const height = Math.floor((source.height * scale) / 2) * 2;
    const capabilities = hdrVideoCapabilities();
    if (!capabilities.supported || width < 16 || height < 16) {
      status({
        mode: "sdr",
        message: `${!capabilities.supported ? capabilities.reason : "当前画面比例无法生成至少 16 像素的 HDR 视频"} · sRGB 预览`,
      });
      return;
    }
    const target = windows.current.get(acceptedChannel)?.contentWindow;
    if (!target) return;
    const abort = new AbortController();
    const captures = new Map<
      number,
      {
        resolve: (data: ArrayBuffer[]) => void;
        buffers: ArrayBuffer[];
        count: number;
        seen: Set<number>;
        cleanup: () => void;
        reject: (cause: Error) => void;
        timer: number;
      }
    >();
    const receive = (event: MessageEvent) => {
      const { channel, type, value } = event.data || {};
      if (
        event.source !== target ||
        channel !== acceptedChannel ||
        type !== "hdr-captured"
      )
        return;
      const request = captures.get(value?.id);
      if (!request) return;
      if (value.error) {
        request.cleanup();
        request.reject(new Error(String(value.error)));
      } else if (
        value.width !== width ||
        value.height !== height ||
        !(value.data instanceof ArrayBuffer) ||
        value.data.byteLength !== width * height * 3 ||
        !Number.isInteger(value.index) ||
        value.index < 0 ||
        value.index >= request.count ||
        request.seen.has(value.index)
      ) {
        request.cleanup();
        request.reject(new Error("HDR 预览批次或像素尺寸不符"));
      } else {
        request.buffers[value.index] = value.data;
        request.seen.add(value.index);
        if (request.seen.size === request.count) {
          request.cleanup();
          request.resolve(request.buffers);
        }
      }
    };
    window.addEventListener("message", receive);
    // The parent owns the native video because WebKit cannot attach MSE in an
    // opaque sandbox. Only requested, bounded float replies cross this boundary.
    const controller = createHDRPreview({
      mount: videoHost.current,
      width,
      height,
      config: {
        frames: source.frames,
        fps: source.fps.numerator / source.fps.denominator,
        frameRate: source.fps,
        display: { working_space: { source: source.source_color_space } },
      },
      capture(indices, signal) {
        return new Promise<ArrayBuffer[]>((resolve, reject) => {
          // Never reuse an ID when an effect remounts on the same iframe.
          const id = ++captureSequence.current;
          const cleanup = () => {
            const entry = captures.get(id);
            if (entry) window.clearTimeout(entry.timer);
            captures.delete(id);
            signal.removeEventListener("abort", cancel);
          };
          const cancel = () => {
            cleanup();
            target.postMessage(
              { channel: acceptedChannel, type: "hdr-cancel", value: id },
              "*",
            );
            reject(new DOMException("HDR capture cancelled", "AbortError"));
          };
          const timer = window.setTimeout(() => {
            cleanup();
            reject(new Error("HDR 批次采集超时"));
          }, 15000);
          captures.set(id, {
            resolve,
            reject,
            timer,
            buffers: [],
            count: indices.length,
            seen: new Set(),
            cleanup,
          });
          signal.addEventListener("abort", cancel, { once: true });
          if (signal.aborted) {
            cancel();
            return;
          }
          target.postMessage(
            {
              channel: acceptedChannel,
              type: "hdr-capture",
              value: { id, frames: indices },
            },
            "*",
          );
        });
      },
      encodeChunk: (header, frames) =>
        encodeHDRPreview(
          header,
          frames,
          AbortSignal.any([abort.signal, AbortSignal.timeout(45000)]),
        ),
      onStatus: status,
      onFrame: setFrame,
      onPlaying: setPlaying,
      onFallback() {
        target.postMessage(
          {
            channel: acceptedChannel,
            type: "seek",
            value: latestFrame.current,
          },
          "*",
        );
      },
    });
    hdr.current = controller;
    controller.seek(Math.min(latestFrame.current, source.frames - 1));
    return () => {
      abort.abort();
      controller.dispose();
      if (hdr.current === controller) hdr.current = null;
      window.removeEventListener("message", receive);
      for (const request of captures.values()) {
        request.cleanup();
        request.reject(new Error("预览已替换"));
      }
      captures.clear();
    };
  }, [acceptedChannel, acceptedRecipe, acceptedHDR]);

  useEffect(() => {
    // A replaced candidate must not let its old readiness timeout overwrite a
    // newer draft's status. Only committed pictures survive a recipe change.
    setPictures((old) => old.filter((item) => item.ready));
    if (!active || composing || !recipe) {
      setPending(false);
      return;
    }
    // Returning to a retained preview (or ending IME without changes) reuses its
    // mounted GPU/media state. Explicit retry and different works still compile.
    if (
      current.current.some(
        (picture) =>
          picture.ready &&
          picture.recipe === recipe &&
          picture.sceneId === sceneId &&
          picture.hdrPreview === hdrPreview &&
          picture.retry === retry,
      )
    ) {
      setPending(false);
      // Failed candidates no longer describe this restored draft. A mounted
      // picture's own runtime error still needs a successful frame to recover.
      setError((old) => (old?.channel ? old : null));
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
          {
            spec: JSON.parse(recipe),
            hdr_preview: hdrPreview && hdrVideoSupport(),
          },
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
              hdrPreview,
              sceneId,
              retry,
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
  }, [recipe, active, composing, sceneId, retry, command, hdrPreview]);

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
      // Escape from the opaque iframe must reach its enclosing large viewer.
      if (data.type === "exit-expanded-preview") panel.current?.close();
      if (
        data.type === "hdr-status" &&
        picture.ready &&
        ["hdr", "sdr", "buffering"].includes(data.value?.mode) &&
        typeof data.value?.message === "string"
      ) {
        setPictures((old) =>
          old.map((item) =>
            item.channel === data.channel
              ? {
                  ...item,
                  hdr: {
                    mode: data.value.mode,
                    message: data.value.message.slice(0, 300),
                  },
                }
              : item,
          ),
        );
      }
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
        if (picture.recipe !== recipe || picture.hdrPreview !== hdrPreview)
          return;
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
            value: Math.min(latestFrame.current, picture.frames - 1),
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
  }, [recipe, hdrPreview]);

  useEffect(() => {
    const pauseHidden = () => {
      if (!active || document.hidden) command("pause");
    };
    pauseHidden();
    document.addEventListener("visibilitychange", pauseHidden);
    return () => document.removeEventListener("visibilitychange", pauseHidden);
  }, [active, command]);

  const stale =
    !!accepted &&
    (accepted.recipe !== recipe || accepted.hdrPreview !== hdrPreview);
  // Retained pictures keep their own rational rate when a replacement draft fails.
  const acceptedSpec = useMemo<MotionSpec | null>(
    () => (acceptedRecipe ? JSON.parse(acceptedRecipe) : null),
    [acceptedRecipe],
  );
  const rate = acceptedSpec
    ? acceptedSpec.fps.numerator / acceptedSpec.fps.denominator
    : 1;
  const maxFrame = (accepted?.frames || spec?.frames || 1) - 1;
  return (
    <PreviewPanel
      ref={panel}
      className="motion-preview-panel"
      label="动画实时预览"
      active={active}
      toolbar={
        <div className="section-heading">
          <h2>实时预览</h2>
          <span className="tiny-label">
            {spec?.engine === "remotion-sdr-v1"
              ? "Remotion · SDR"
              : accepted?.hdr?.mode === "hdr"
                ? "ACES · HDR PQ"
                : accepted?.hdr?.mode === "buffering"
                  ? "ACES · HDR 缓冲"
                  : "ACES · sRGB"}
          </span>
        </div>
      }
    >
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
        <div ref={videoHost} className="motion-hdr-video" />
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
                ? accepted.hdr?.message || "当前草稿 · 网页 sRGB 预览"
                : "等待代码预览"}
      </div>
      <PreviewTimeline
        animation
        frame={frame}
        maxFrame={maxFrame}
        playing={playing}
        disabled={!accepted}
        onPlay={() => {
          if (!playing) audio.current?.unlock();
          command(playing ? "pause" : "play");
        }}
        onSeek={(value) => {
          command("pause");
          command("seek", value);
        }}
        tools={
          <>
            {spec?.engine === "three-float-v1" && (
              <Field label="预览显示" id="motion-preview-display">
                <select
                  id="motion-preview-display"
                  value={hdrPreview ? "auto" : "srgb"}
                  onChange={(event) =>
                    setHdrPreview(event.target.value === "auto")
                  }
                >
                  <option value="auto">自动 HDR</option>
                  <option value="srgb">sRGB SDR</option>
                </select>
              </Field>
            )}
            <Button
              onClick={() => setRetry((old) => old + 1)}
              disabled={pending || !spec}
              aria-label="重新编译动画"
              title="重新编译动画"
            >
              <RefreshCw size={15} />
            </Button>
          </>
        }
      />
      <PreviewAudio
        key={`${projectId}:${sceneId}`}
        ref={audio}
        projectId={projectId}
        active={active}
        playing={
          playing &&
          !!accepted &&
          !error?.channel &&
          accepted.hdr?.mode !== "buffering"
        }
        frameDuration={1 / rate}
        seconds={frame / rate}
        duration={(maxFrame + 1) / rate}
        clockKey={`${sceneId}:${accepted?.channel}`}
      />
      <Notice text={error?.message || ""} error />
      {/* Keep color/precision guidance available without reserving a paragraph of viewing space. */}
      <details className="technical-note preview-notes">
        <summary>预览说明</summary>
        <p>
          {spec?.engine === "remotion-sdr-v1"
            ? "SDR 网页画面 · sRGB 8-bit。保留网页图层与原始动画代码。"
            : "宽色域浮点场景在支持的设备上使用 720p HDR 视频按项目帧率预览，否则回退 sRGB。视频预览合成黑底；ACEScg 母版独立保存。"}
        </p>
      </details>
    </PreviewPanel>
  );
});
