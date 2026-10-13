import { useState } from "react";
import { FolderOpen, FolderSearch } from "lucide-react";
import { call } from "./api";
import { Button, Notice } from "./components";

/** Shared native-folder actions retain drafts on cancellation and local errors. */
export function FolderPickerButton({
  initialDirectory,
  available,
  disabled = false,
  onSelected,
  onPickingChange,
}: {
  initialDirectory: string;
  available: boolean;
  disabled?: boolean;
  onSelected: (directory: string) => void;
  onPickingChange?: (picking: boolean) => void;
}) {
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState("");
  async function choose() {
    setPicking(true);
    setError("");
    onPickingChange?.(true);
    try {
      const reply = await call<{
        cancelled: boolean;
        directory: string | null;
      }>("choose_export_directory", {
        initial_directory: initialDirectory || null,
      });
      if (!reply.cancelled && reply.directory) onSelected(reply.directory);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPicking(false);
      onPickingChange?.(false);
    }
  }
  return (
    <div className="export-folder-action">
      <Button
        disabled={disabled || picking || !available}
        aria-busy={picking}
        title={
          available
            ? "打开系统文件夹选择器"
            : "当前服务未提供文件夹选择器，可直接输入路径。"
        }
        onClick={() => void choose()}
      >
        <FolderSearch size={14} />
        选择文件夹
      </Button>
      <Notice text={error} error />
    </div>
  );
}

/** Callers expose this only after the exported folder has been published. */
export function OpenExportFolder({ directory }: { directory: string }) {
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState("");
  async function open() {
    setOpening(true);
    setError("");
    try {
      await call("open_export_directory", { directory });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setOpening(false);
    }
  }
  return (
    <div className="export-folder-action">
      <Button
        disabled={opening}
        aria-busy={opening}
        onClick={() => void open()}
      >
        <FolderOpen size={14} />
        打开导出文件夹
      </Button>
      <Notice text={error} error />
    </div>
  );
}
