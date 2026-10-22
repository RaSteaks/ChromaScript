import { useEffect, useMemo, useRef, useState } from "react";
import { call } from "./api";
import type { Scene } from "./types";

export type LivePreviewRequest = {
  spec: Scene;
  width: number;
  height: number;
  fps: { numerator: number; denominator: number };
  frame: number;
};
export type LivePreviewReply = {
  images: {
    data: string;
    mime_type: "image/png";
    width: number;
    height: number;
    bit_depth: 8;
  }[];
  frame: number;
  samples: 1;
  transient: true;
};
type PreviewState = {
  sceneId: string;
  attempt: string;
  imageRecipe: string;
  reply: LivePreviewReply | null;
  pending: boolean;
  error: string;
};

// Playback may skip timeline frames; shader time still comes from each exact
// frame index and rational fps. A slow GPU never builds a request backlog.
export const LIVE_PLAYBACK_INTERVAL = 1000 / 24;
const EDIT_DELAY = 180;

export function useLivePreview(
  sceneId: string,
  request: LivePreviewRequest | null,
  enabled: boolean,
  playing: boolean,
) {
  const [state, setState] = useState<PreviewState | null>(null);
  const [retry, setRetry] = useState(0);
  const [visible, setVisible] = useState(() => !document.hidden);
  const inFlight = useRef<Promise<LivePreviewReply> | null>(null);
  const latestFrame = useRef(0);
  // During playback, changing time must not invalidate an otherwise current
  // render: even a GPU slower than the refresh cap must be able to show frames.
  const spec = request?.spec;
  const width = request?.width;
  const height = request?.height;
  const numerator = request?.fps.numerator;
  const denominator = request?.fps.denominator;
  const stillFrame = playing ? undefined : request?.frame;
  const recipe = useMemo(
    () =>
      spec
        ? JSON.stringify({
            spec,
            width,
            height,
            fps: { numerator, denominator },
            frame: stillFrame,
          })
        : "",
    [spec, width, height, numerator, denominator, stillFrame],
  );
  const active = enabled && visible && !!recipe;

  useEffect(() => {
    latestFrame.current = request?.frame ?? 0;
  }, [request?.frame]);

  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let timer: number;
    const render = async () => {
      // Aborting fetch does not stop native GPU work on the server. Wait for
      // that work, then send only the newest draft; discard every stale reply.
      if (inFlight.current) await inFlight.current.catch(() => {});
      if (stopped) return;
      const started = performance.now();
      setState((old) => ({
        sceneId,
        attempt: recipe,
        imageRecipe: old?.sceneId === sceneId ? old.imageRecipe : "",
        reply: old?.sceneId === sceneId ? old.reply : null,
        pending: true,
        error: "",
      }));
      const task = call<LivePreviewReply>(
        "render_live_preview",
        {
          request: playing
            ? { ...JSON.parse(recipe), frame: latestFrame.current }
            : JSON.parse(recipe),
        },
        AbortSignal.timeout(15000),
      );
      inFlight.current = task;
      try {
        const reply = await task;
        if (!stopped)
          setState({
            sceneId,
            attempt: recipe,
            imageRecipe: recipe,
            reply,
            pending: false,
            error: "",
          });
        if (!stopped && playing)
          timer = window.setTimeout(
            () => void render(),
            // Rendering time counts toward the refresh budget; never add a full
            // interval after a slow frame or overlap native GPU requests.
            Math.max(0, LIVE_PLAYBACK_INTERVAL - (performance.now() - started)),
          );
      } catch (error) {
        if (!stopped)
          setState((old) => ({
            sceneId,
            attempt: recipe,
            imageRecipe: old?.sceneId === sceneId ? old.imageRecipe : "",
            reply: old?.sceneId === sceneId ? old.reply : null,
            pending: false,
            error:
              error instanceof Error && error.name === "TimeoutError"
                ? "实时预览超时。请简化场景代码后重试。"
                : error instanceof Error
                  ? error.message
                  : String(error),
          }));
      } finally {
        if (inFlight.current === task) inFlight.current = null;
      }
    };
    timer = window.setTimeout(() => void render(), EDIT_DELAY);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [active, playing, recipe, retry, sceneId]);

  const current = state?.sceneId === sceneId ? state : null;
  return {
    active,
    reply: current?.reply ?? null,
    loading: active && (current?.attempt !== recipe || !!current?.pending),
    stale: !!current?.reply && current.imageRecipe !== recipe,
    error: current?.attempt === recipe ? current.error : "",
    retry: () => setRetry((value) => value + 1),
  };
}
