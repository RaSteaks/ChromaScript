import { useEffect, useRef, useState, type PointerEvent } from "react";
import { Pipette } from "lucide-react";
import { call } from "./api";
import {
  fitChroma,
  inGamut,
  oklchToLinear,
  paletteColor,
  swatchCss,
} from "./appearance";
import { Button, Field, Modal, Notice } from "./components";
import type { ColorAppearance, OklchColor, Parameter } from "./types";

export type ColorInputDraft = { key: string; channels: string[] };

// Share the float32 input boundary with workspace save/unsaved-change protection.
export function validColorChannels(channels: readonly string[]) {
  return (
    channels.length === 3 &&
    channels.every(
      (v) =>
        v.trim() !== "" &&
        Number.isFinite(Number(v)) &&
        Math.abs(Number(v)) <= 3.4028234663852886e38,
    )
  );
}

type Props = {
  id: string;
  label: string;
  value: number[];
  colorSpace: Parameter["color_space"];
  disabled: boolean;
  rawDraft: ColorInputDraft | null;
  onRawDraftChange: (draft: ColorInputDraft | null) => void;
  onChange: (rgb: number[]) => void;
};

/** One owner for scene color fields; opening/inspection never changes scene data. */
export function ColorField({
  id,
  label,
  value,
  colorSpace,
  disabled,
  rawDraft,
  onRawDraftChange,
  onChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const [retry, setRetry] = useState(0);
  const [inspection, setInspection] = useState<{
    key: string;
    result?: ColorAppearance;
    error?: string;
  } | null>(null);
  const [red, green, blue] = value;
  const key = JSON.stringify([colorSpace, red, green, blue]);
  // The workspace owns raw input because switching workflow views unmounts this field.
  const raw = rawDraft?.key === key ? rawDraft.channels : value.map(String);
  const rawInvalid = !validColorChannels(raw);
  useEffect(() => {
    const controller = new AbortController();
    call<ColorAppearance>(
      "resolve_color",
      {
        request: { rgb: [red, green, blue], color_space: colorSpace },
      },
      controller.signal,
    )
      .then((result) => {
        if (!controller.signal.aborted) setInspection({ key, result });
      })
      .catch((error: Error) => {
        if (!controller.signal.aborted)
          setInspection({ key, error: error.message });
      });
    return () => controller.abort();
  }, [key, red, green, blue, colorSpace, retry]);
  const current = inspection?.key === key ? inspection : null;

  return (
    <Field
      label={label}
      id={id}
      hint={`参数存储：${colorSpace} · OKLCH 感知选色`}
    >
      <Button
        id={id}
        className="color-trigger outline"
        disabled={disabled}
        aria-label={`${label} 选取颜色`}
        aria-describedby={id + "-help"}
        onClick={() => setOpen(true)}
      >
        <span
          className="color-chip"
          aria-hidden="true"
          style={
            current?.result
              ? { background: swatchCss(current.result.display_rgb) }
              : undefined
          }
        />
        <span>
          选取颜色
          <small>
            {current?.result
              ? "ACES 显示色样"
              : current?.error
                ? "预览不可用"
                : "预览加载中"}
          </small>
        </span>
        <Pipette size={16} aria-hidden="true" />
      </Button>
      {current?.error && (
        <div className="color-error">
          <Notice error text={`颜色预览不可用：${current.error}`} />
          <Button onClick={() => setRetry((n) => n + 1)}>重试颜色预览</Button>
        </div>
      )}
      <details className="color-advanced">
        <summary>高级 RGB · {colorSpace}</summary>
        <div className="rgb-inputs">
          {["R", "G", "B"].map((channel, index) => (
            <label key={channel}>
              <span>{channel}</span>
              <input
                aria-label={`${label} ${channel}`}
                type="number"
                step="any"
                value={raw[index]}
                disabled={disabled}
                aria-invalid={rawInvalid}
                aria-describedby={rawInvalid ? id + "-rgb-error" : undefined}
                onChange={(event) => {
                  // Incomplete channels stay in the workspace draft across view
                  // changes; never replace an unfinished float with zero.
                  const channels = [...raw];
                  channels[index] = event.target.value;
                  const valid = validColorChannels(channels);
                  const next = channels.map(Number);
                  onRawDraftChange({
                    key: valid ? JSON.stringify([colorSpace, ...next]) : key,
                    channels,
                  });
                  if (valid) onChange(next);
                }}
              />
            </label>
          ))}
        </div>
        {/* Parameter encoding is separate from the shader's fixed linear space. */}
        <small>
          保留负值与大于 1 的浮点值。参数进入代码前统一转换为线性 ACEScg。
        </small>
        {rawInvalid && (
          <small id={id + "-rgb-error"} role="alert">
            请输入三个有限的浮点数；未完成的输入尚未应用。
          </small>
        )}
      </details>
      {open && (
        <ColorPicker
          label={label}
          value={value}
          colorSpace={colorSpace}
          disabled={disabled}
          onClose={() => setOpen(false)}
          onApply={(rgb) => {
            onRawDraftChange(null);
            onChange(rgb);
            setOpen(false);
          }}
        />
      )}
    </Field>
  );
}

/** The modal holds its own candidate until Apply; Escape/cancel preserve exact RGB. */
function ColorPicker({
  label,
  value,
  colorSpace,
  disabled,
  onClose,
  onApply,
}: {
  label: string;
  value: number[];
  colorSpace: Parameter["color_space"];
  disabled: boolean;
  onClose: () => void;
  onApply: (rgb: number[]) => void;
}) {
  const [original, setOriginal] = useState<ColorAppearance | null>(null);
  const [candidate, setCandidate] = useState<OklchColor | null>(null);
  const [resolved, setResolved] = useState<{
    key: string;
    result: ColorAppearance;
  } | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [loadRetry, setLoadRetry] = useState(0);
  const [red, green, blue] = value;
  const requestKey = JSON.stringify(candidate);
  useEffect(() => {
    const controller = new AbortController();
    call<ColorAppearance>(
      "resolve_color",
      {
        request: { rgb: [red, green, blue], color_space: colorSpace },
      },
      controller.signal,
    )
      .then((result) => {
        if (controller.signal.aborted) return;
        setOriginal(result);
        setCandidate(result.oklch);
        setError("");
      })
      .catch((reason: Error) => {
        if (!controller.signal.aborted) setError(reason.message);
      });
    return () => controller.abort();
  }, [red, green, blue, colorSpace, loadRetry]);

  const edited =
    candidate !== null &&
    original !== null &&
    JSON.stringify(candidate) !== JSON.stringify(original.oklch);
  useEffect(() => {
    if (!candidate || !edited) return;
    const controller = new AbortController();
    // Local drawing is immediate. Only the latest debounced server conversion
    // can enable Apply, so delayed responses never write an older selection.
    const timer = window.setTimeout(() => {
      call<ColorAppearance>(
        "resolve_color",
        {
          request: { oklch: candidate, color_space: colorSpace },
        },
        controller.signal,
      )
        .then((result) => {
          if (!controller.signal.aborted) {
            setResolved({ key: requestKey, result });
            setError("");
          }
        })
        .catch((reason: Error) => {
          if (!controller.signal.aborted) setError(reason.message);
        });
    }, 100);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [candidate, requestKey, edited, colorSpace, retry]);

  const result = edited
    ? resolved?.key === requestKey
      ? resolved.result
      : null
    : original;
  const mapped = candidate ? !inGamut(oklchToLinear(candidate)) : false;
  const change = (patch: Partial<OklchColor>) => {
    setError("");
    setCandidate((old) => (old ? { ...old, ...patch } : old));
  };
  return (
    <Modal open title={`${label} · 感知选色`} onClose={onClose}>
      <div
        className="color-picker"
        aria-busy={!error && (!original || (edited && !result))}
      >
        <p className="color-picker-intro">OKLCH · 按色相、明度和彩度选取颜色</p>
        <p className="color-picker-description">
          色样使用 sRGB / ACES 2.0 SDR 预览。应用后写入 {colorSpace}{" "}
          颜色参数；进入代码前统一转换为线性
          ACEScg。应用仅更新草稿，保存仍需手动操作。
        </p>
        {candidate ? (
          <>
            <ColorPlane
              color={candidate}
              onChange={change}
              disabled={disabled}
              descriptionId="color-plane-help"
            />
            <small id="color-plane-help">
              向右增加彩度，向上提高明度。也可用下方滑块和方向键调整。
            </small>
            <div className="color-sliders">
              {(
                [
                  ["hue", "色相 H", 360, 1, "°"],
                  ["lightness", "明度 L", 1, 0.001, "%"],
                  ["chroma", "彩度 C", 0.4, 0.001, ""],
                ] as const
              ).map(([name, caption, max, step, unit]) => {
                const shown =
                  name === "lightness"
                    ? (candidate[name] * 100).toFixed(1)
                    : candidate[name].toFixed(name === "chroma" ? 3 : 0);
                const gradient = Array.from({ length: 19 }, (_, i) =>
                  swatchCss(
                    paletteColor({ ...candidate, [name]: (i * max) / 18 }),
                  ),
                );
                return (
                  <Field key={name} id={`picker-${name}`} label={caption}>
                    <div className="color-slider-row">
                      <input
                        id={`picker-${name}`}
                        type="range"
                        min={0}
                        max={max}
                        step={step}
                        value={candidate[name]}
                        disabled={disabled}
                        aria-valuetext={shown + unit}
                        style={{
                          backgroundImage: `linear-gradient(to right, ${gradient.join(",")})`,
                        }}
                        onChange={(e) =>
                          change({ [name]: Number(e.target.value) })
                        }
                      />
                      <output htmlFor={`picker-${name}`} className="mono">
                        {shown}
                        {unit}
                      </output>
                    </div>
                  </Field>
                );
              })}
            </div>
            <div className="color-comparison">
              <div>
                <span
                  className="color-sample"
                  style={{ background: swatchCss(original!.display_rgb) }}
                />
                <span>原颜色</span>
              </div>
              <div>
                <span
                  className="color-sample"
                  style={{
                    background: swatchCss(
                      result?.display_rgb ?? paletteColor(candidate),
                    ),
                  }}
                />
                <span>
                  新颜色{edited && !result && !error ? " · 换算中" : ""}
                </span>
              </div>
            </div>
            <div className="color-picker-status" role="status">
              {mapped ? (
                <p className="warning-text">
                  超出 sRGB 色域：保持明度与色相，降低彩度
                  {result?.gamut_mapped
                    ? `至 ${result.oklch.chroma.toFixed(3)}`
                    : "至可显示范围"}
                  。
                </p>
              ) : (
                <p>当前选择在 sRGB 色域内。</p>
              )}
              {original?.preview_clipped && (
                <p>原颜色超出显示范围，色样已限幅；原始 RGB 保持不变。</p>
              )}
              {original?.extended_range && (
                <p>原始值含负值或大于 1 的数值；打开或取消不会改写。</p>
              )}
            </div>
          </>
        ) : (
          <div className="color-picker-loading" role="status">
            {error ? "颜色预览暂不可用，请重试。" : "正在读取颜色预览…"}
          </div>
        )}
        {error && (
          <div className="color-error">
            <Notice error text={`选色换算失败：${error}`} />
            <Button
              onClick={() => {
                setError("");
                if (original) setRetry((n) => n + 1);
                else setLoadRetry((n) => n + 1);
              }}
            >
              重试选色换算
            </Button>
          </div>
        )}
      </div>
      <div className="modal-actions">
        <Button onClick={onClose}>取消</Button>
        <Button
          className="primary"
          disabled={disabled || !edited || !result || !!error}
          onClick={() => {
            if (result && edited && !error) onApply(result.rgb);
          }}
        >
          应用颜色
        </Button>
      </div>
    </Modal>
  );
}

function ColorPlane({
  color,
  onChange,
  disabled,
  descriptionId,
}: {
  color: OklchColor;
  onChange: (patch: Partial<OklchColor>) => void;
  disabled: boolean;
  descriptionId: string;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const node = canvas.current,
      ctx = node?.getContext("2d");
    if (!node || !ctx) return;
    const image = ctx.createImageData(node.width, node.height);
    for (let y = 0; y < node.height; y++) {
      // One boundary search per row keeps hue dragging responsive, rather than
      // repeating 28 bisections for every out-of-gamut pixel in the canvas.
      const lightness = 1 - y / (node.height - 1);
      const maximum = fitChroma({ lightness, chroma: 0.4, hue: color.hue });
      for (let x = 0; x < node.width; x++) {
        const chroma = (x / (node.width - 1)) * 0.4;
        const sample = {
          lightness,
          chroma: Math.min(chroma, maximum),
          hue: color.hue,
        };
        // Out-of-gamut areas keep their mapped color and receive a hatch; the
        // boundary is visual, while the text status exposes the same information.
        const rgb = paletteColor(sample);
        const hatch = chroma > maximum && (x + y) % 10 < 2;
        const i = (y * node.width + x) * 4;
        for (let c = 0; c < 3; c++)
          image.data[i + c] = rgb[c] * 255 * (hatch ? 0.72 : 1);
        image.data[i + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
  }, [color.hue]);
  function choose(event: PointerEvent<HTMLButtonElement>) {
    if (disabled) return;
    const box = event.currentTarget.getBoundingClientRect();
    onChange({
      chroma:
        Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)) * 0.4,
      lightness:
        1 - Math.max(0, Math.min(1, (event.clientY - box.top) / box.height)),
    });
  }
  return (
    <button
      type="button"
      className="color-plane"
      disabled={disabled}
      aria-label="明度与彩度选色板"
      aria-describedby={descriptionId}
      onPointerDown={(event) => {
        if (disabled || event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        choose(event);
      }}
      onPointerMove={(event) => {
        if (
          event.buttons === 1 &&
          event.currentTarget.hasPointerCapture(event.pointerId)
        )
          choose(event);
      }}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing) return;
        const increment = event.shiftKey ? 0.05 : 0.01;
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault();
          onChange({
            chroma: Math.max(
              0,
              Math.min(
                0.4,
                color.chroma +
                  (event.key === "ArrowRight" ? 1 : -1) * increment * 0.4,
              ),
            ),
          });
        } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
          event.preventDefault();
          onChange({
            lightness: Math.max(
              0,
              Math.min(
                1,
                color.lightness +
                  (event.key === "ArrowUp" ? 1 : -1) * increment,
              ),
            ),
          });
        }
      }}
    >
      <canvas ref={canvas} width={240} height={160} aria-hidden="true" />
      <span
        className="color-plane-marker"
        aria-hidden="true"
        style={{
          left: `${(color.chroma / 0.4) * 100}%`,
          top: `${(1 - color.lightness) * 100}%`,
        }}
      />
    </button>
  );
}
