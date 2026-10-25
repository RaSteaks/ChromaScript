import { useEffect, useId, useRef, type ReactNode } from "react";
import { Pause, Play, SkipBack, StepBack, StepForward } from "lucide-react";
import { Button } from "./components";

/** Both renderers seek integer frames; their adapters retain the exact rational clock. */
export function PreviewTimeline({
  frame,
  maxFrame,
  playing,
  disabled,
  onPlay,
  onSeek,
  onScrubbingChange,
  animation = false,
  tools,
}: {
  frame: number;
  maxFrame: number;
  playing: boolean;
  disabled: boolean;
  onPlay: () => void;
  onSeek: (frame: number) => void;
  onScrubbingChange?: (scrubbing: boolean) => void;
  animation?: boolean;
  tools?: ReactNode;
}) {
  const id = useId();
  const finishDrag = useRef<(() => void) | null>(null);
  useEffect(() => () => finishDrag.current?.(), []);
  const last = Number.isFinite(maxFrame) ? Math.max(0, maxFrame) : 0;
  const current = Number.isFinite(frame)
    ? Math.max(0, Math.min(frame, last))
    : 0;
  return (
    <div className="preview-timeline" aria-label="预览播放控制">
      <div className="preview-timeline-actions">
        <Button
          disabled={disabled || last === 0}
          onClick={onPlay}
          aria-pressed={playing}
          aria-label={
            animation
              ? playing
                ? "暂停动画"
                : "播放动画"
              : playing
                ? "暂停实时预览"
                : "播放实时预览"
          }
        >
          {playing ? <Pause size={15} /> : <Play size={15} />}
          {playing ? "暂停" : "播放"}
        </Button>
        <Button
          disabled={disabled}
          aria-label={animation ? "回到动画首帧" : "回到首帧"}
          onClick={() => onSeek(0)}
        >
          <SkipBack size={15} />
        </Button>
        <Button
          disabled={disabled || current === 0}
          aria-label={animation ? "上一帧动画" : "上一帧"}
          onClick={() => onSeek(Math.max(0, current - 1))}
        >
          <StepBack size={15} />
        </Button>
        <Button
          disabled={disabled || current === last}
          aria-label={animation ? "下一帧动画" : "下一帧"}
          onClick={() => onSeek(Math.min(last, current + 1))}
        >
          <StepForward size={15} />
        </Button>
        <span className="mono">
          {current} / {last}
        </span>
      </div>
      <label className="sr-only" htmlFor={id}>
        {animation ? "动画时间线" : "实时预览播放位置"}
      </label>
      <input
        id={id}
        type="range"
        min={0}
        max={last}
        step={1}
        value={current}
        disabled={disabled}
        // Leave native thumb tracking to the browser. Explicit pointer capture
        // steals WebKit's internal thumb drag; window listeners still catch
        // release outside the control and clean up on cancellation/unmount.
        onPointerDown={() => {
          finishDrag.current?.();
          const finish = () => {
            window.removeEventListener("pointerup", finish);
            window.removeEventListener("pointercancel", finish);
            finishDrag.current = null;
            onScrubbingChange?.(false);
          };
          finishDrag.current = finish;
          window.addEventListener("pointerup", finish, { once: true });
          window.addEventListener("pointercancel", finish, { once: true });
          onScrubbingChange?.(true);
        }}
        onBlur={() => finishDrag.current?.()}
        onKeyDown={() => finishDrag.current?.()}
        onChange={(e) => onSeek(Number(e.target.value))}
      />
      {/* Display/recompile tools share the transport, without moving the media DOM. */}
      {tools && <div className="preview-timeline-tools">{tools}</div>}
    </div>
  );
}
