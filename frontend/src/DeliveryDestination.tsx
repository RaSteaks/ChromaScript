import { FolderPlus } from "lucide-react";
import { Button, Field, Notice } from "./components";
import type { Capability } from "./types";

type Props = {
  options: Capability["delivery_export"];
  value: string;
  error: string;
  disabled: boolean;
  onChange: (value: string) => void;
};

/** One parent directory belongs to the entire delivery, including every output format. */
export function DeliveryDestination({
  options,
  value,
  error,
  disabled,
  onChange,
}: Props) {
  return (
    <section className="delivery-destination" aria-label="交付导出位置">
      <div className="section-heading divider">
        <h3>
          <FolderPlus size={14} />
          导出位置与文件
        </h3>
        <Button
          disabled={disabled || !options}
          onClick={() => onChange(options!.default_directory)}
        >
          使用默认位置
        </Button>
      </div>
      <Field
        label="导出位置"
        id="delivery-directory"
        invalid={!!error}
        hint={
          error ||
          "输入父文件夹的绝对路径。每次交付会自动新建“场景名-任务编号”文件夹。"
        }
      >
        <input
          id="delivery-directory"
          type="text"
          value={value}
          disabled={disabled || !options}
          spellCheck={false}
          autoComplete="off"
          aria-invalid={!!error}
          aria-describedby="delivery-directory-help"
          onChange={(event) => onChange(event.target.value)}
        />
      </Field>
      <p className="panel-description">
        文件夹包含场景代码（WGSL）、完整场景（JSON）、sRGB
        缩略图和所选视频或图像序列，附带验证报告。
      </p>
      {options ? (
        <details className="export-roots">
          <summary>可用导出目录</summary>
          <p>
            支持以下目录及其子文件夹。使用其他位置时，请通过启动参数
            --export-root 配置后重启服务。
          </p>
          <ul>
            {options.allowed_roots.map((root) => (
              <li key={root}>
                <Button disabled={disabled} onClick={() => onChange(root)}>
                  {root}
                </Button>
              </li>
            ))}
          </ul>
        </details>
      ) : (
        <Notice
          error
          text="本地服务尚未提供交付导出位置，请更新并重启服务后重新连接。"
        />
      )}
    </section>
  );
}
