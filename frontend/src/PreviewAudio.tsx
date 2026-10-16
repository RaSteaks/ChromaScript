import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useId,
  useRef,
  useState,
  type Ref,
} from "react";
import { Volume2, VolumeX } from "lucide-react";
import { artifactUrl, call } from "./api";
import { Button, Notice } from "./components";

type Clip = {
  asset_id: string;
  start_seconds: number;
  source_start_seconds: number;
  duration_seconds: number | null;
  gain_db: number;
};
type Track = {
  project_id: string;
  revision_id: string;
  timeline: { clips: Clip[] };
  assets: { id: string; duration_seconds: number }[];
};
type AudioClock = {
  key: string;
  seconds: number;
  started: number;
  duration: number;
  frameDuration: number;
};
type ClipPlayback = { clock: AudioClock; cycle: number; positionedAt: number };
export type PreviewAudioHandle = {
  unlock: () => void;
  seek: (seconds: number) => void;
};

/** One transport for both preview engines; media streams avoid decoding whole long tracks into RAM. */
export function PreviewAudio({
  projectId,
  active,
  playing,
  seconds,
  duration,
  frameDuration,
  clockKey,
  ref,
}: {
  projectId: string;
  active: boolean;
  playing: boolean;
  seconds: number;
  duration: number;
  frameDuration: number;
  clockKey: string;
  ref: Ref<PreviewAudioHandle>;
}) {
  const [track, setTrack] = useState<Track | null>(null);
  const [error, setError] = useState("");
  const [volume, setVolume] = useState(100);
  const volumeId = useId();
  const volumeValue = useRef(volume);
  volumeValue.current = volume;
  const masterGain = useRef<GainNode | null>(null);
  const [retry, setRetry] = useState(0);
  const [ready, setReady] = useState(false);
  const context = useRef<AudioContext | null>(null);
  const media = useRef(new Map<number, HTMLAudioElement>());
  const gains = useRef(new WeakMap<HTMLAudioElement, GainNode>());
  const sources = useRef(
    new WeakMap<HTMLAudioElement, MediaElementAudioSourceNode>(),
  );
  const attempts = useRef(new WeakSet<HTMLAudioElement>());
  const sync = useRef(() => {});
  const clock = useRef<AudioClock | null>(null);
  const positions = useRef(new WeakMap<HTMLAudioElement, ClipPlayback>());
  const latestSeconds = useRef(seconds);
  latestSeconds.current = seconds;
  const stop = useCallback(
    () => media.current.forEach((audio) => audio.pause()),
    [],
  );
  const bind = useCallback((index: number, node: HTMLAudioElement | null) => {
    const old = media.current.get(index);
    if (old && old !== node) {
      old.pause();
      gains.current.get(old)?.disconnect();
      sources.current.get(old)?.disconnect();
      old.removeAttribute("src");
      old.load();
    }
    if (node) media.current.set(index, node);
    else media.current.delete(index);
  }, []);
  const unlock = useCallback(() => {
    try {
      context.current ??= new AudioContext();
      if (!masterGain.current) {
        masterGain.current = context.current.createGain();
        masterGain.current.gain.value = volumeValue.current / 100;
        masterGain.current.connect(context.current.destination);
      }
      void context.current
        .resume()
        .then(() => {
          setReady(true);
          sync.current();
        })
        .catch(() => {
          setError("浏览器未允许声音播放，请点击重试预览音频。");
        });
    } catch {
      setError("无法启用声音，请检查浏览器音频支持后重试。");
    }
  }, []);
  const seek = useCallback((position: number) => {
    // User seeks are explicit discontinuities. Sparse rendered-frame messages
    // are not seeks and must never repeatedly reset a long WAV's decoder.
    latestSeconds.current = position;
    clock.current = null;
    sync.current();
  }, []);
  useImperativeHandle(ref, () => ({ unlock, seek }), [unlock, seek]);

  useEffect(() => {
    const ctx = context.current;
    const gain = masterGain.current?.gain;
    if (!ctx || !gain) return;
    // Monitor volume is separate from saved clip gain. A short ramp avoids
    // clicks; changing volume never seeks or restarts the playback clock.
    gain.cancelScheduledValues(ctx.currentTime);
    gain.setTargetAtTime(volume / 100, ctx.currentTime, 0.01);
  }, [volume]);

  useEffect(() => {
    if (!active || !projectId) return;
    const abort = new AbortController();
    let timer: number;
    const load = async () => {
      try {
        const next = await call<Track>(
          "get_project_audio",
          { project_id: projectId },
          AbortSignal.any([abort.signal, AbortSignal.timeout(10000)]),
        );
        if (!abort.signal.aborted) {
          // Poll agent saves without touching the audio editor's unsaved draft.
          setTrack((old) =>
            old?.project_id === next.project_id &&
            old.revision_id === next.revision_id
              ? old
              : next,
          );
        }
      } catch (cause) {
        if (!abort.signal.aborted) {
          stop();
          setTrack(null);
          setError(
            `音轨读取失败：${cause instanceof Error ? cause.message : String(cause)}`,
          );
        }
      } finally {
        if (!abort.signal.aborted)
          timer = window.setTimeout(() => void load(), 2000);
      }
    };
    void load();
    return () => {
      abort.abort();
      window.clearTimeout(timer);
    };
  }, [projectId, active, retry, stop]);

  const current = track?.project_id === projectId ? track : null;
  useEffect(() => {
    // Anchor once to the visual transport, then use the audio device clock.
    // Frame acknowledgements can be delayed by GPU work; they must not cause
    // the former 500 ms watchdog pauses or restart the decoder every frame.
    const update = () => {
      const ctx = context.current;
      const audible = active && playing && ready && !error && !document.hidden;
      if (!audible || !ctx || !Number.isFinite(duration) || duration <= 0) {
        clock.current = null;
        stop();
        return;
      }
      if (ctx.state !== "running") return;
      let transport = clock.current;
      if (
        !transport ||
        transport.key !== clockKey ||
        transport.duration !== duration ||
        transport.frameDuration !== frameDuration
      ) {
        transport = {
          key: clockKey,
          seconds: latestSeconds.current,
          started: ctx.currentTime,
          duration,
          frameDuration,
        };
        clock.current = transport;
      }
      const absolute = transport.seconds + ctx.currentTime - transport.started;
      const cycle = Math.floor(absolute / duration);
      const time = absolute % duration;
      media.current.forEach((audio, index) => {
        const clip = current?.timeline.clips[index];
        const asset = current?.assets.find(
          (item) => item.id === clip?.asset_id,
        );
        if (!clip || !asset) {
          positions.current.delete(audio);
          audio.pause();
          return;
        }
        const length =
          clip.duration_seconds ??
          asset.duration_seconds - clip.source_start_seconds;
        const offset = time - clip.start_seconds;
        if (offset < 0 || offset >= length) {
          positions.current.delete(audio);
          audio.pause();
          return;
        }
        if (audio.readyState < 1) return;
        let gain = gains.current.get(audio);
        if (!gain) {
          gain = ctx.createGain();
          const source = ctx.createMediaElementSource(audio);
          source.connect(gain).connect(masterGain.current!);
          sources.current.set(audio, source);
          gains.current.set(audio, gain);
        }
        gain.gain.value = 10 ** (clip.gain_db / 20);
        const target = clip.source_start_seconds + offset;
        const position = positions.current.get(audio);
        const entered =
          !position || position.clock !== transport || position.cycle !== cycle;
        // Seek once on entry/resume/loop/user seek. While buffering or seeking,
        // allow the in-flight range request/decoder to finish instead of chasing
        // a target that advances every 25 ms. Only settled playback can recover
        // a large residual drift, with a five-second cooldown between corrections.
        // Ordinary decoder/output latency is tolerated; playback rate stays unchanged.
        const recover =
          position &&
          !audio.paused &&
          !audio.seeking &&
          audio.readyState >= 3 &&
          ctx.currentTime - position.positionedAt >= 5 &&
          Math.abs(audio.currentTime - target) > 2;
        if (entered || recover) {
          positions.current.set(audio, {
            clock: transport,
            cycle,
            positionedAt: ctx.currentTime,
          });
          if (Math.abs(audio.currentTime - target) > 0.03)
            audio.currentTime = target;
        }
        if (audio.paused && !attempts.current.has(audio)) {
          attempts.current.add(audio);
          void audio
            .play()
            .catch((cause: DOMException) => {
              if (
                cause.name !== "AbortError" &&
                media.current.get(index) === audio
              ) {
                stop();
                setError(
                  "音频播放失败，请点击重试预览音频，或检查项目音频素材。",
                );
              }
            })
            .finally(() => attempts.current.delete(audio));
        }
      });
    };
    sync.current = update;
    update();
    // Paused/inactive previews do no periodic audio work.
    const timer =
      active && playing && ready && !error
        ? window.setInterval(update, 25)
        : undefined;
    const visibility = () => {
      update();
      if (document.hidden) window.clearInterval(timer);
    };
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [
    current,
    active,
    playing,
    duration,
    frameDuration,
    clockKey,
    ready,
    error,
    stop,
  ]);

  useEffect(() => {
    // Capture nodes: React clears refs before passive unmount cleanup.
    const nodes = media.current;
    return () => {
      nodes.forEach((audio) => audio.pause());
      void context.current?.close();
    };
  }, []);

  return (
    <div className="preview-audio" aria-label="预览音频">
      {(current?.timeline.clips ?? []).map((clip, index) => (
        <PreviewClip
          key={`${projectId}:${current!.revision_id}:${index}`}
          bind={bind}
          index={index}
          src={artifactUrl(clip.asset_id)}
          onReady={() => sync.current()}
          onError={() =>
            setError("音频素材无法读取或解码，请检查项目音频后重试。")
          }
        />
      ))}
      <div className="preview-audio-controls">
        <div className="preview-audio-volume">
          <label htmlFor={volumeId}>
            {volume === 0 ? (
              <VolumeX size={16} aria-hidden="true" />
            ) : (
              <Volume2 size={16} aria-hidden="true" />
            )}
            预览音量
          </label>
          <input
            id={volumeId}
            type="range"
            min={0}
            max={100}
            step={1}
            value={volume}
            disabled={!current?.timeline.clips.length || !!error}
            aria-valuetext={volume === 0 ? "静音，0%" : `${volume}%`}
            onChange={(event) => {
              unlock();
              setVolume(Number(event.target.value));
            }}
          />
          <span className="preview-audio-percentage mono" aria-hidden="true">
            {volume}%
          </span>
        </div>
        {!current?.timeline.clips.length && (
          <span className="technical-note" aria-live="polite">
            {!current
              ? error
                ? "音轨不可用"
                : "正在读取项目音轨…"
              : "项目未设置音轨"}
          </span>
        )}
        {!!error && (
          <Button
            onClick={() => {
              setError("");
              setRetry((value) => value + 1);
              media.current.forEach((audio) => audio.load());
              unlock();
            }}
          >
            重试预览音频
          </Button>
        )}
      </div>
      <Notice error text={error} />
    </div>
  );
}

/** Stable refs prevent React frame updates from reloading an already playing stream. */
function PreviewClip({
  index,
  bind,
  src,
  onReady,
  onError,
}: {
  index: number;
  bind: (index: number, node: HTMLAudioElement | null) => void;
  src: string;
  onReady: () => void;
  onError: () => void;
}) {
  const attach = useCallback(
    (node: HTMLAudioElement | null) => bind(index, node),
    [bind, index],
  );
  return (
    <audio
      ref={attach}
      src={src}
      preload="auto"
      hidden
      onLoadedMetadata={onReady}
      onError={onError}
    />
  );
}
