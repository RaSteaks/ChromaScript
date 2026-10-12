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
  ["color_output", "输出色彩"],
  ["data_range", "数据范围"],
  ["compression", "图像压缩"],
  ["quality_mode", "质量控制"],
];

/** Native dropdowns share the server's constraints; no preset IDs are selected or saved. */
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
      {delivery.targets.map((target, index) => {
        const { configuration: config, resolved } = target;
        const prefix = `delivery-${target.id}`;
        return (
          <fieldset
            key={target.id}
            className="delivery-target"
            aria-busy={target.pending}
          >
            <legend>交付目标 {index + 1}</legend>
            <div className="target-actions">
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
                    <Field key={field} label={label} id={`${prefix}-${field}`}>
                      <select
                        id={`${prefix}-${field}`}
                        value={config[field] ?? ""}
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
                          >
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </Field>
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
                : resolved && !target.error && resolved.adjustments.length > 0
                  ? "已联动调整：" +
                    resolved.adjustments
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
