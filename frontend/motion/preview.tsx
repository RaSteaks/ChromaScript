import { Component, useEffect, useRef, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { Player, type PlayerRef } from "@remotion/player";
import Scene from "chromascript:scene";
import config from "chromascript:config";

// The bridge contains playback state only; no editor token, path or API is exposed.
function send(type: string, value: unknown = null) {
  parent.postMessage({ channel: config.channel, type, value }, "*");
}

class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: Error) {
    send("error", error.message);
  }
  render() {
    return this.state.failed ? null : this.props.children;
  }
}

function Preview() {
  const player = useRef<PlayerRef>(null);
  useEffect(() => {
    const ref = player.current;
    if (!ref) return;
    const frame = ({ detail }: { detail: { frame: number } }) =>
      send("frame", detail.frame);
    const play = () => send("playing", true);
    const pause = () => send("playing", false);
    const command = (event: MessageEvent) => {
      if (event.source !== parent || event.data?.channel !== config.channel)
        return;
      if (event.data.type === "play") ref.play();
      if (event.data.type === "pause") ref.pause();
      if (event.data.type === "seek" && Number.isInteger(event.data.value))
        ref.seekTo(Math.max(0, Math.min(config.frames - 1, event.data.value)));
    };
    ref.addEventListener("frameupdate", frame);
    ref.addEventListener("play", play);
    ref.addEventListener("pause", pause);
    window.addEventListener("message", command);
    // Report readiness after the first committed picture. Compile/runtime errors
    // leave the editor's previous successful iframe mounted and usable.
    // Hidden/offscreen iframes may suspend requestAnimationFrame. Readiness is
    // a successful React commit, so it must not depend on animation playback.
    let ready = 0;
    let paint = 0;
    let announced = false;
    const announce = () => {
      if (!announced) {
        announced = true;
        send("ready");
      }
    };
    const checkLayout = () => {
      const rect = ref.getContainerNode()?.getBoundingClientRect();
      const expected = rect
        ? Math.min(rect.width / config.width, rect.height / config.height)
        : 0;
      // ResizeObserver must have fitted the composition before it replaces the
      // previous picture, including when the source is smaller than the preview.
      if (expected > 0 && Math.abs(ref.getScale() - expected) < 0.002) {
        // Commit two paints after layout so Chromium updates the iframe clip and
        // WebGL surface together. Offscreen frames retain a bounded timer fallback.
        paint = requestAnimationFrame(() => {
          paint = requestAnimationFrame(announce);
        });
        ready = window.setTimeout(announce, 250);
      } else ready = window.setTimeout(checkLayout, 50);
    };
    ready = window.setTimeout(checkLayout, 100);
    return () => {
      window.clearTimeout(ready);
      cancelAnimationFrame(paint);
      ref.removeEventListener("frameupdate", frame);
      ref.removeEventListener("play", play);
      ref.removeEventListener("pause", pause);
      window.removeEventListener("message", command);
    };
  }, []);
  return (
    <Player
      ref={player}
      component={Scene}
      compositionWidth={config.width}
      compositionHeight={config.height}
      durationInFrames={config.frames}
      fps={config.fps}
      controls={false}
      autoPlay={false}
      loop
      style={{ width: "100%", height: "100%" }}
      errorFallback={({ error }) => {
        send("error", error.message);
        return null;
      }}
    />
  );
}

window.addEventListener("error", (event) => send("error", event.message));
window.addEventListener("unhandledrejection", (event) =>
  send("error", String(event.reason?.message || event.reason)),
);
createRoot(document.getElementById("root")!).render(
  <Boundary>
    <Preview />
  </Boundary>,
);
