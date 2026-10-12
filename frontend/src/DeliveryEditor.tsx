import { Fragment } from "react";
import { ArrowDownToLine, Plus, RefreshCw, X } from "lucide-react";
import { Button, Field, Notice } from "./components";
import type { useDeliveryTargets } from "./delivery";
import type { DeliveryConfiguration, DeliverySelectField } from "./types";

type Props = {
  delivery: ReturnType<typeof useDeliveryTargets>;
  disabled: boolean;
};
const FIELDS: [DeliverySelectField, string][] = [
  ["container", "封装 / 图像格式"],
  ["codec", "视频 / 图像编码"],
  ["profile", "编码档位"],
  ["bit_depth", "位深"],
  ["sampling", "色度采样"],
  ["alpha_mode", "Alpha"],
  ["color_space", "输出色彩空间"],
  ["gamma", "传递函数 / Gamma"],
  ["data_range", "数据级别"],
  ["compression", "图像压缩"],
  ["quality_mode", "质量控制"],
];

const GROUP_TITLES: Partial<Record<DeliverySelectField, string>> = {
  container: "格式与编码",
  color_space: "色彩管理与数据级别",
  compression: "压缩与质量",
};

/** Resolve-style output controls transform pixels; file tags describe that same encoding. */
export function DeliveryEditor({ delivery, disabled }: Props) {
  return (
    <section className="delivery-editor" aria-label="目标交付格式">
      <div className="section-heading divider">
        <span>
          <ArrowDownToLine size={14} />
          目标交付格式
        </span>
        <Button
          disabled={disabled || delivery.targets.length >= 20}
          onClick={delivery.add}
          aria-label="添加交付目标"
        >
          <Plus size={14} />
          添加
        </Button>
      </div>
      <p className="panel-description">
        先选封装与编码，再检查色彩、精度和质量。可添加不同格式同时交付。
      </p>
      {delivery.targets.map((target, index) => {
        const { configuration: config, resolved } = target;
        const prefix = `delivery-${target.id}`;
        // Only the shared server's separated color contract is presented here.
        // Updating the compatibility alias is bookkeeping, not another user choice.
        const adjustments =
          resolved?.adjustments.filter(
            (item) => item.field !== "color_output",
          ) ?? [];
        return (
          <fieldset
            key={target.id}
            className="delivery-target"
            aria-busy={target.pending}
          >
            <legend>交付目标 {index + 1}</legend>
            <div className="target-actions">
              <strong className="target-summary">
                {["container", "codec", "bit_depth"]
                  .map(
                    (field) =>
                      resolved?.options[field as DeliverySelectField].find(
                        (item) =>
                          item.value === config[field as DeliverySelectField],
                      )?.label,
                  )
                  .filter(Boolean)
                  .join(" · ") || "正在读取可选格式…"}
              </strong>
              <Button
                disabled={disabled || delivery.targets.length === 1}
                aria-label={`移除交付目标 ${index + 1}`}
                onClick={() => delivery.remove(target.id)}
              >
                <X size={13} />
              </Button>
            </div>
            {resolved && (
              <div className="delivery-fields">
                {FIELDS.map(([field, label]) => {
                  const options = resolved.options[field];
                  return (
                    <Fragment key={field}>
                      {/* Group titles explain decisions; option compatibility remains server-owned. */}
                      {GROUP_TITLES[field] && (
                        <h4 className="delivery-group-title">
                          {GROUP_TITLES[field]}
                        </h4>
                      )}
                      {field === "color_space" && (
                        <p className="color-encoding-summary">
                          {resolved.color_encoding.profile_label} ·{" "}
                          {resolved.color_encoding.white_point}
                        </p>
                      )}
                      <Field
                        label={label}
                        id={`${prefix}-${field}`}
                        hint={
                          field === "color_space"
                            ? resolved.color_space_note
                            : field === "gamma"
                              ? resolved.gamma_note
                              : field === "data_range"
                                ? resolved.data_range_note || undefined
                                : undefined
                        }
                      >
                        {/* Disabled choices explain format limits; keep multi-entry menus inspectable. */}
                        <select
                          id={`${prefix}-${field}`}
                          value={config[field] ?? ""}
                          aria-describedby={
                            (field === "color_space" &&
                              resolved.color_space_note) ||
                            (field === "gamma" && resolved.gamma_note) ||
                            (field === "data_range" && resolved.data_range_note)
                              ? `${prefix}-${field}-help`
                              : undefined
                          }
                          disabled={
                            disabled ||
                            (target.pending && field !== "container") ||
                            options.length < 2 ||
                            (!!target.error && field !== "container")
                          }
                          onChange={(event) => {
                            const raw = event.target.value;
                            const value =
                              field === "bit_depth"
                                ? Number(raw)
                                : (field === "profile" ||
                                      field === "compression") &&
                                    raw === ""
                                  ? null
                                  : raw;
                            delivery.change(
                              target.id,
                              field,
                              value as DeliveryConfiguration[typeof field],
                            );
                          }}
                        >
                          {options.map((option) => (
                            <option
                              key={String(option.value)}
                              value={option.value ?? ""}
                              disabled={option.disabled}
                            >
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </Field>
                      {field === "gamma" && (
                        <>
                          <Field
                            label="目标显示峰值亮度"
                            id={`${prefix}-target-peak`}
                            hint={
                              resolved.color_encoding.dynamic_range.startsWith(
                                "scene-",
                              )
                                ? "场景编码母版不烘焙显示输出变换。"
                                : "由当前 ACES 输出变换固定；与色域、传递函数分别记录。"
                            }
                          >
                            <input
                              id={`${prefix}-target-peak`}
                              type="text"
                              readOnly
                              value={
                                resolved.color_encoding
                                  .target_display_peak_nits === null
                                  ? resolved.color_encoding.dynamic_range ===
                                    "scene-log"
                                    ? "不适用（ACEScct 对数）"
                                    : "不适用（场景线性）"
                                  : `${resolved.color_encoding.target_display_peak_nits} cd/m²（nit）`
                              }
                              aria-describedby={`${prefix}-target-peak-help`}
                            />
                          </Field>
                          {/* Tags follow the actual output, rather than offering a second color conversion. */}
                          <Field
                            label="色彩空间标签"
                            id={`${prefix}-space-tag`}
                          >
                            <input
                              id={`${prefix}-space-tag`}
                              readOnly
                              value={resolved.file_tags.color_space}
                            />
                          </Field>
                          <Field label="Gamma 标签" id={`${prefix}-gamma-tag`}>
                            <input
                              id={`${prefix}-gamma-tag`}
                              readOnly
                              value={resolved.file_tags.gamma}
                            />
                          </Field>
                        </>
                      )}
                    </Fragment>
                  );
                })}
                {(["crf", "bitrate_kbps", "gop_seconds"] as const)
                  .filter((field) =>
                    field === "gop_seconds"
                      ? config.quality_mode !== "fixed"
                      : field === "crf"
                        ? config.quality_mode === "crf"
                        : config.quality_mode === "bitrate",
                  )
                  .map((field) => {
                    const bounds = resolved.numeric_fields[field];
                    const value =
                      target.numbers[field] ?? String(config[field] ?? "");
                    const number = Number(value);
                    const invalid =
                      !value.trim() ||
                      !Number.isFinite(number) ||
                      number < bounds.min ||
                      number > bounds.max ||
                      (field !== "gop_seconds" && !Number.isInteger(number));
                    const label =
                      field === "crf"
                        ? "CRF"
                        : field === "bitrate_kbps"
                          ? "目标码率（kb/s）"
                          : "关键帧间隔（秒）";
                    return (
                      <Field
                        key={field}
                        label={label}
                        id={`${prefix}-${field}`}
                        invalid={invalid}
                        hint={
                          invalid
                            ? `请输入 ${bounds.min}–${bounds.max} 的数值。`
                            : undefined
                        }
                      >
                        <input
                          id={`${prefix}-${field}`}
                          type="number"
                          value={value ?? ""}
                          min={bounds.min}
                          max={bounds.max}
                          step={bounds.step}
                          disabled={disabled}
                          aria-invalid={invalid}
                          aria-describedby={
                            invalid ? `${prefix}-${field}-help` : undefined
                          }
                          onChange={(event) =>
                            delivery.changeNumber(
                              target.id,
                              field,
                              event.target.value,
                            )
                          }
                        />
                      </Field>
                    );
                  })}
              </div>
            )}
            <div className="delivery-state" role="status">
              {target.pending
                ? "正在更新可选格式…"
                : resolved && !target.error && adjustments.length > 0
                  ? "已联动调整：" +
                    adjustments
                      .map((item) => {
                        const options =
                          resolved.options[item.field as DeliverySelectField] ||
                          [];
                        const label =
                          options.find((option) => option.value === item.value)
                            ?.label ??
                          (item.value === null ? "不适用" : String(item.value));
                        return item.label + "→" + label;
                      })
                      .join("、") +
                    "。"
                  : resolved && !target.error
                    ? "交付参数已同步。"
                    : ""}
            </div>
            {target.error && (
              <div className="delivery-error">
                <Notice text={target.error} error />
                <Button
                  disabled={disabled || target.pending}
                  onClick={() => delivery.retry(target.id)}
                >
                  <RefreshCw size={13} />
                  重试联动选项
                </Button>
              </div>
            )}
            {resolved?.color_note && (
              <p className="technical-note">{resolved.color_note}</p>
            )}
            {resolved?.precision_note && (
              <p className="technical-note">{resolved.precision_note}</p>
            )}
          </fieldset>
        );
      })}
      {delivery.duplicate && (
        <Notice text="交付设置重复，请调整或移除重复目标。" error />
      )}
    </section>
  );
}
