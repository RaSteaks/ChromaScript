import { FolderPlus } from "lucide-react";
import { Button, Field } from "./components";
import { FolderPickerButton } from "./ExportFolders";
import type { Capability } from "./types";

type Props = {
  options: Capability["delivery_export"];
  defaultDirectory?: string;
  value: string;
  error: string;
  disabled: boolean;
  onChange: (value: string) => void;
  variant?: "scene" | "animation";
  videoOnly?: boolean;
  onPickingChange?: (picking: boolean) => void;
};

/** One parent directory belongs to the entire delivery, including every output format. */
export function DeliveryDestination({
  options,
  defaultDirectory,
  value,
  error,
  disabled,
  onChange,
  variant = "scene",
  videoOnly = false,
  onPickingChange,
}: Props) {
  // Both engines share path validation and controls, but their source bundles differ.
  const id =
    variant === "animation" ? "motion-directory" : "delivery-directory";
  // Initial selection and explicit reset share the selected project's default.
  const resetDirectory = defaultDirectory ?? options?.default_directory;
  return (
    <section className="delivery-destination" aria-label="交付导出位置">
      <div className="section-heading divider">
        <h3>
          <FolderPlus size={14} />
          导出位置与文件
        </h3>
        <div className="destination-actions">
          <FolderPickerButton
            initialDirectory={value}
            available={options?.directory_picker === true}
            disabled={disabled}
            onSelected={onChange}
            onPickingChange={onPickingChange}
          />
          <Button
            disabled={disabled || !resetDirectory}
            onClick={() => resetDirectory && onChange(resetDirectory)}
          >
            使用默认位置
          </Button>
        </div>
      </div>
      <Field
        label="导出位置"
        id={id}
        invalid={!!error}
        hint={
          error ||
          `选择文件夹，或直接输入可写的绝对路径。每次交付会新建“${variant === "animation" ? "动画" : "场景"}名-任务编号”文件夹。`
        }
      >
        <input
          id={id}
          type="text"
          value={value}
          disabled={disabled}
          spellCheck={false}
          autoComplete="off"
          aria-invalid={!!error}
          aria-describedby={id + "-help"}
          onChange={(event) => onChange(event.target.value)}
        />
      </Field>
      <p className="panel-description">
        {variant === "animation"
          ? // Describe only the artifacts selected for this delivery.
            videoOnly
            ? "成片和临时文件直接写入所选磁盘；仅视频模式不写入 EXR 母版，不缓存整段浮点帧。文件夹包含视频、源码、规格、sRGB 缩略图及验证报告。"
            : "可选择外部硬盘；动画帧、中间文件和成片直接写入所选磁盘，本机工作区仅保留任务记录与日志。文件夹包含源码、规格、sRGB 缩略图、所选视频及验证报告；选择保留母版时另含 ACEScg EXR 序列，ZIP 可按需下载。"
          : "文件夹包含场景代码（WGSL）、完整场景（JSON）、sRGB 缩略图和所选视频或图像序列，附带验证报告。"}
      </p>
    </section>
  );
}
