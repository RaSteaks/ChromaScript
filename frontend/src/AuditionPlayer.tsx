import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pause, Play, Volume2 } from "lucide-react";
import { artifactUrl, call } from "./api";
import { Button, Notice } from "./components";
import { MotionPreview } from "./MotionPreview";
import { useLivePreview } from "./livePreview";
import type { MotionSpec, Scene } from "./types";
import type { Audition } from "./auditions";

/** Per-candidate review state shown on the comparison strip. */
export type CandidateMark = {
  text: string;
  tone: "reference" | "reject" | "unsure" | "none";
  draft: boolean;
};

const WAVEFORM_BUCKETS = 600;

/** Peak envelope of the bounded excerpt only. Display aid, not a meter: values
 * are sample peaks (linear full scale), never LUFS or true peak. */
function envelope(buffer: AudioBuffer, seconds: number) {
  const out = new Float32Array(WAVEFORM_BUCKETS);
  const frames = Math.min(
    buffer.length,
    Math.round(seconds * buffer.sampleRate),
  );
  const size = Math.max(1, Math.floor(frames / WAVEFORM_BUCKETS));
  for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
    const data = buffer.getChannelData(channel);
    for (let bucket = 0; bucket < WAVEFORM_BUCKETS; bucket++) {
      let peak = out[bucket];
      const end = Math.min(frames, (bucket + 1) * size);
      for (let i = bucket * size; i < end; i++) {
        const value = Math.abs(data[i]);
        if (value > peak) peak = value;
      }
      out[bucket] = peak;
    }
  }
  return out;
}

/** Bounded excerpt transport. All sources share one device clock; choosing a
 * candidate only ramps gains, never seeks the picture or writes a timeline. */
export function AuditionPlayer({
  round,
  selected,
  onSelect,
  marks,
  active,
}: {
  round: Audition;
  selected: string;
  onSelect: (id: string) => void;
  marks: Record<string, CandidateMark>;
  active: boolean;
}) {
  const context = useRef<AudioContext | null>(null);
  const buffers = useRef(new Map<string, AudioBuffer>());
  const sources = useRef<
    { source: AudioBufferSourceNode; gain: GainNode; id: string }[]
  >([]);
  const activeValue = useRef(active);
  activeValue.current = active;
  const playEpoch = useRef(0);
  const starting = useRef(false);
  const anchor = useRef<{ time: number; offset: number } | null>(null);
  const offset = useRef(0);
  // A drag or key-repeat seek pauses once and resumes on release, so the
  // shared clock restarts a single time per gesture instead of per input event.
  const resumeAfterSeek = useRef(false);
  const [seconds, setSeconds] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loaded, setLoaded] = useState<string[]>([]);
  const [peaks, setPeaks] = useState<Record<string, Float32Array>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [loop, setLoop] = useState(true);
  const [normalize, setNormalize] = useState(true);
  const [volume, setVolume] = useState(80);
  const loopValue = useRef(loop);
  loopValue.current = loop;
  const excerpts = JSON.stringify([
    ...round.candidates.map((c) => ({
      id: c.id,
      artifact: c.playback_artifact_id,
    })),
    ...(round.baseline.playback_artifact_id
      ? [{ id: "baseline", artifact: round.baseline.playback_artifact_id }]
      : []),
  ]);
  const stop = useCallback(() => {
    playEpoch.current++;
    starting.current = false;
    const a = anchor.current;
    if (a && context.current) {
      const elapsed = a.offset + context.current.currentTime - a.time;
      offset.current = loopValue.current
        ? Math.max(0, elapsed) % round.duration_seconds
        : Math.max(0, Math.min(elapsed, round.duration_seconds));
    }
    anchor.current = null;
    sources.current.forEach(({ source, gain }) => {
      source.stop();
      source.disconnect();
      gain.disconnect();
    });
    sources.current = [];
    setSeconds(offset.current);
    setPlaying(false);
  }, [round.duration_seconds]);

  useEffect(() => {
    const abort = new AbortController();
    const ctx = new AudioContext();
    context.current = ctx;
    buffers.current.clear();
    setLoading(true);
    setLoaded([]);
    setPeaks({});
    setError("");
    const items: { id: string; artifact: string }[] = JSON.parse(excerpts);
    void Promise.allSettled(
      items.map(async (item) => {
        const response = await fetch(artifactUrl(item.artifact), {
          signal: AbortSignal.any([abort.signal, AbortSignal.timeout(20000)]),
        });
        if (!response.ok) throw new Error("片段读取失败");
        const buffer = await ctx.decodeAudioData(await response.arrayBuffer());
        if (!abort.signal.aborted) buffers.current.set(item.id, buffer);
      }),
    ).then((results) => {
      if (abort.signal.aborted) return;
      setLoaded([...buffers.current.keys()]);
      setPeaks(
        Object.fromEntries(
          [...buffers.current].map(([id, buffer]) => [
            id,
            envelope(buffer, round.duration_seconds),
          ]),
        ),
      );
      setLoading(false);
      if (results.some((r) => r.status === "rejected"))
        setError(
          "部分片段未能准备，无法播放的候选已禁用。可重试，当前选择和反馈会保留。",
        );
    });
    return () => {
      abort.abort();
      sources.current.forEach(({ source, gain }) => {
        source.stop();
        source.disconnect();
        gain.disconnect();
      });
      sources.current = [];
      anchor.current = null;
      void ctx.close();
      context.current = null;
    };
    // A revision only changes feedback; immutable excerpt identities do not reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [excerpts, retry]);

  useEffect(() => {
    if (!active) stop();
    const hide = () => {
      if (document.hidden) stop();
    };
    document.addEventListener("visibilitychange", hide);
    return () => document.removeEventListener("visibilitychange", hide);
  }, [active, stop]);

  const gainOf = useCallback(
    (id: string) => {
      const candidate =
        round.candidates.find((c) => c.id === id) ?? round.baseline;
      return 10 ** ((normalize ? candidate.gain_db : 0) / 20);
    },
    [round, normalize],
  );
  useEffect(() => {
    const ctx = context.current;
    if (!ctx) return;
    for (const item of sources.current) {
      const gain = item.id === selected ? (volume / 100) * gainOf(item.id) : 0;
      item.source.loop = loop;
      item.gain.gain.cancelScheduledValues(ctx.currentTime);
      item.gain.gain.setTargetAtTime(gain, ctx.currentTime, 0.008);
    }
  }, [selected, gainOf, volume, loop, playing]);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      const a = anchor.current,
        ctx = context.current;
      if (!a || !ctx) return;
      const elapsed = a.offset + ctx.currentTime - a.time;
      if (!loopValue.current && elapsed >= round.duration_seconds) {
        stop();
        return;
      }
      setSeconds(Math.max(0, elapsed) % round.duration_seconds);
    }, 40);
    return () => window.clearInterval(timer);
  }, [playing, round.duration_seconds, stop]);

  async function play() {
    const ctx = context.current;
    if (!ctx || !active || starting.current || anchor.current) return;
    starting.current = true;
    const started = playEpoch.current;
    try {
      await ctx.resume();
      if (
        context.current !== ctx ||
        document.hidden ||
        !activeValue.current ||
        started !== playEpoch.current
      )
        return;
      if (offset.current >= round.duration_seconds) offset.current = 0;
      const time = ctx.currentTime + 0.02;
      anchor.current = { time, offset: offset.current };
      buffers.current.forEach((buffer, id) => {
        const source = ctx.createBufferSource(),
          gain = ctx.createGain();
        source.buffer = buffer;
        source.loop = loop;
        source.loopEnd = round.duration_seconds;
        gain.gain.value = 0;
        source.connect(gain).connect(ctx.destination);
        source.start(time, offset.current);
        sources.current.push({ source, gain, id });
      });
      setPlaying(true);
    } catch {
      setError("浏览器未允许音频播放，请再次点击播放。");
    } finally {
      starting.current = false;
    }
  }
  const seek = (value: number) => {
    if (playing || anchor.current) resumeAfterSeek.current = true;
    stop();
    offset.current = value;
    setSeconds(value);
  };
  const commitSeek = () => {
    if (!resumeAfterSeek.current) return;
    resumeAfterSeek.current = false;
    void play();
  };
  const available = (id: string) =>
    loaded.includes(id) ||
    (id === "baseline" && !round.baseline.playback_artifact_id);
  const canPlay = !loading && available(selected);
  const toggle = () => (playing ? stop() : void play());
  const shortcutCount = Math.min(round.candidates.length, 9);
  return (
    <section
      aria-label="试听播放器"
      className="audition-player"
      tabIndex={0}
      onKeyDown={(event) => {
        if (
          (event.target as HTMLElement).closest(
            "input, textarea, select, button",
          ) ||
          event.nativeEvent.isComposing ||
          event.ctrlKey ||
          event.altKey ||
          event.metaKey
        )
          return;
        if (event.key === " ") {
          event.preventDefault();
          if (canPlay) toggle();
          return;
        }
        const id =
          event.key === "0"
            ? "baseline"
            : round.candidates[Number(event.key) - 1]?.id;
        if (id && available(id)) {
          event.preventDefault();
          onSelect(id);
        }
      }}
    >
      {round.context && (
        <AuditionPicture
          round={round}
          seconds={seconds}
          playing={playing}
          active={active}
        />
      )}
      <div className="audition-transport">
        <Button
          className="primary audition-play"
          disabled={!canPlay}
          onClick={toggle}
        >
          {playing ? (
            <Pause size={16} aria-hidden="true" />
          ) : (
            <Play size={16} aria-hidden="true" />
          )}
          {playing ? "暂停试听" : "播放试听"}
        </Button>
        <span className="audition-time" role="status">
          {loading
            ? "正在准备音频…"
            : `${seconds.toFixed(1)} / ${round.duration_seconds.toFixed(1)} 秒`}
        </span>
      </div>
      {/* The waveform is decoration; the transparent native range on top owns
          pointer, keyboard and accessibility semantics. */}
      <div
        className={
          "audition-scrubber" + (round.context ? "" : " audition-scrubber-tall")
        }
      >
        <Waveform
          peaks={peaks[selected]}
          gain={gainOf(selected)}
          progress={seconds / round.duration_seconds}
        />
        <label className="sr-only" htmlFor="audition-position">
          试听位置（秒）
        </label>
        <input
          id="audition-position"
          type="range"
          min={0}
          max={round.duration_seconds}
          step={0.01}
          value={seconds}
          onChange={(e) => seek(Number(e.target.value))}
          onPointerUp={commitSeek}
          onKeyUp={commitSeek}
          onBlur={commitSeek}
        />
      </div>
      <div
        className="audition-candidates"
        role="group"
        aria-label={`对比候选（数字键 1–${shortcutCount} 切换，0 为对照）`}
      >
        {round.candidates.map((candidate, index) => {
          const mark = marks[candidate.id];
          return (
            <Button
              key={candidate.id}
              aria-pressed={selected === candidate.id}
              className={
                "audition-candidate" +
                (selected === candidate.id ? " selected" : "")
              }
              data-tone={mark?.tone ?? "none"}
              disabled={!available(candidate.id)}
              onClick={() => onSelect(candidate.id)}
            >
              <span className="audition-candidate-key">{index + 1}</span>
              <span className="sr-only"> · </span>
              <span className="audition-candidate-label">
                {candidate.label}
              </span>
              {candidate.name && (
                <span className="audition-candidate-name">
                  {candidate.name}
                </span>
              )}
              <span className="audition-candidate-mark">
                {mark?.text ?? "未反馈"}
                {mark?.draft ? " · 未保存" : ""}
              </span>
            </Button>
          );
        })}
        <Button
          aria-pressed={selected === "baseline"}
          className={
            "audition-candidate audition-baseline" +
            (selected === "baseline" ? " selected" : "")
          }
          disabled={!available("baseline")}
          onClick={() => onSelect("baseline")}
        >
          <span className="audition-candidate-key">0</span>
          <span className="sr-only"> · </span>
          <span className="audition-candidate-label">
            {round.baseline.playback_artifact_id ? "原配乐" : "无配乐"}
          </span>
          <span className="audition-candidate-mark">对照</span>
        </Button>
      </div>
      <div className="audition-options">
        <label>
          <input
            type="checkbox"
            checked={loop}
            onChange={(e) => setLoop(e.target.checked)}
          />
          循环片段
        </label>
        <label>
          <input
            type="checkbox"
            checked={normalize}
            onChange={(e) => setNormalize(e.target.checked)}
          />
          统一响度
        </label>
        <span className="audition-volume">
          <Volume2 size={15} aria-hidden="true" />
          <label htmlFor="audition-volume">监听音量</label>
          <input
            id="audition-volume"
            type="range"
            min={0}
            max={100}
            value={volume}
            onChange={(e) => setVolume(Number(e.target.value))}
          />
        </span>
      </div>
      <p className="technical-note">
        匹配目标 {round.effective_target_lufs.toFixed(1)} LUFS ·
        切换候选不重置播放位置 · 空格播放/暂停（焦点在播放器区域时）
      </p>
      {round.candidates.some((c) => !c.normalization_available) && (
        <Notice text="部分片段没有有效 LUFS，按原增益播放，不能视为已统一响度。" />
      )}
      <Notice error text={error} />
      {!!error && (
        <Button
          onClick={() => {
            stop();
            setRetry((n) => n + 1);
          }}
        >
          重试试听音频
        </Button>
      )}
    </section>
  );
}

/** Canvas peak overview of the selected excerpt with the played span tinted. */
function Waveform({
  peaks,
  gain,
  progress,
}: {
  peaks: Float32Array | undefined;
  gain: number;
  progress: number;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const node = canvas.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) =>
      setSize({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      }),
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const node = canvas.current;
    const draw = node?.getContext("2d");
    if (!node || !draw || !size.width) return;
    const ratio = window.devicePixelRatio || 1;
    node.width = Math.round(size.width * ratio);
    node.height = Math.round(size.height * ratio);
    draw.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw.clearRect(0, 0, size.width, size.height);
    const style = getComputedStyle(node);
    const played = style.getPropertyValue("--color-primary").trim();
    const rest = style.getPropertyValue("--color-scrollbar").trim();
    const middle = size.height / 2;
    const head = Math.max(0, Math.min(1, progress)) * size.width;
    for (let x = 0; x < size.width; x += 2) {
      const bucket = Math.min(
        WAVEFORM_BUCKETS - 1,
        Math.floor((x / size.width) * WAVEFORM_BUCKETS),
      );
      // Peaks are clamped to full scale; gain mirrors the monitored level.
      const amplitude = peaks ? Math.min(1, peaks[bucket] * gain) : 0;
      const height = Math.max(1, amplitude * (size.height - 8));
      draw.fillStyle = x < head ? played : rest;
      draw.fillRect(x, middle - height / 2, 1, height);
    }
    draw.fillStyle = style.getPropertyValue("--color-text").trim();
    draw.fillRect(Math.min(head, size.width - 1), 0, 1, size.height);
  }, [peaks, gain, progress, size]);
  return <canvas ref={canvas} aria-hidden="true" />;
}

/** Fixed saved picture follows the device clock; candidate changes never replace it. */
function AuditionPicture({
  round,
  seconds,
  playing,
  active,
}: {
  round: Audition;
  seconds: number;
  playing: boolean;
  active: boolean;
}) {
  const [picture, setPicture] = useState<{ scene: Scene | MotionSpec } | null>(
    null,
  );
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const contextKey = JSON.stringify(round.context);
  const ctx = useMemo<Audition["context"]>(
    () => JSON.parse(contextKey),
    [contextKey],
  );
  useEffect(() => {
    setPicture(null);
    setError("");
    if (!ctx) return;
    const abort = new AbortController();
    void call<{ scene: Scene | MotionSpec }>(
      ctx.work_kind === "motion" ? "get_motion_scene" : "get_scene",
      { scene_id: ctx.scene_id, revision_id: ctx.revision_id },
      AbortSignal.any([abort.signal, AbortSignal.timeout(10000)]),
    )
      .then((reply) => {
        if (!abort.signal.aborted) setPicture(reply);
      })
      .catch((cause) => {
        if (!abort.signal.aborted) setError(String(cause));
      });
    return () => abort.abort();
  }, [ctx, retry]);
  const frame = ctx
    ? ctx.start_frame +
      Math.min(
        ctx.frames - 1,
        Math.max(
          0,
          Math.floor((seconds * ctx.fps.numerator) / ctx.fps.denominator),
        ),
      )
    : 0;
  const live = useLivePreview(
    ctx?.scene_id ?? "",
    ctx?.work_kind === "wgsl" && picture
      ? {
          spec: picture.scene as Scene,
          width: 480,
          height: 270,
          frame,
          fps: ctx.fps,
        }
      : null,
    active,
    playing,
  );
  if (!ctx) return null;
  return (
    <div className="audition-picture-frame">
      {!picture && !error && (
        <p className="panel-description" role="status">
          正在读取固定版本画面…
        </p>
      )}
      {picture && ctx.work_kind === "motion" && (
        <MotionPreview
          spec={picture.scene as MotionSpec}
          sceneId={ctx.scene_id}
          projectId={round.project_id}
          active={active}
          composing={false}
          externalFrame={frame}
        />
      )}
      {ctx.work_kind === "wgsl" && live.reply?.images[0] && (
        <img
          className="audition-picture"
          alt="固定版本画面试听"
          src={`data:image/png;base64,${live.reply.images[0].data}`}
        />
      )}
      <Notice error text={error || live.error} />
      {!!error && (
        <Button onClick={() => setRetry((n) => n + 1)}>重试试听画面</Button>
      )}
    </div>
  );
}
