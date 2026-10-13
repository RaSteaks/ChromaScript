import { FolderPlus } from "lucide-react";
import { Button, Field } from "./components";
import { FolderPickerButton } from "./ExportFolders";
import type { Capability } from "./types";

type Props = {
  options: Capability["delivery_export"];
  value: string;
  error: string;
  disabled: boolean;
  onChange: (value: string) => void;
  variant?: "scene" | "animation";
  onPickingChange?: (picking: boolean) => void;
};

/** One parent directory belongs to the entire delivery, including every output format. */
export function DeliveryDestination({
  options,
  value,
  error,
  disabled,
  onChange,
  variant = "scene",
  onPickingChange,
}: Props) {
  // Both engines share path validation and controls, but their source bundles differ.
  const id =
    variant === "animation" ? "motion-directory" : "delivery-directory";
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
            disabled={disabled || !options}
            onClick={() => onChange(options!.default_directory)}
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
          ? "文件夹包含动画代码（TSX）、完整规格（JSON）、sRGB 缩略图和 MP4 视频，附带验证报告。"
          : "文件夹包含场景代码（WGSL）、完整场景（JSON）、sRGB 缩略图和所选视频或图像序列，附带验证报告。"}
      </p>
    </section>
  );
}
