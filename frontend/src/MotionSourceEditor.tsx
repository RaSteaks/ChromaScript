import { useState } from "react";
import { Button, Field, Modal } from "./components";
import { CodeEditor } from "./CodeEditor";
import type { MotionSpec } from "./types";

export function sourceFilesError(
  code: string,
  files: Record<string, string>,
): string {
  if (
    Object.keys(files).length > 128 ||
    code.length + Object.values(files).reduce((n, v) => n + v.length, 0) >
      2000000
  )
    return "最多支持 128 个分章文件，全部源码合计最多 2000000 字符。";
  const names = new Set(["animation.ts", "animation.tsx"]);
  const directories = new Map<string, string>();
  for (const [name, source] of Object.entries(files)) {
    if (
      name.length > 180 ||
      !/^(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.(?:ts|tsx|js|jsx)$/.test(name) ||
      names.has(name.toLowerCase())
    )
      return `分章路径无效、重复或与入口冲突：${name}。使用英文字母、数字、下划线或连字符命名。`;
    names.add(name.toLowerCase());
    // Match backend/compiler checks before a case-insensitive export can merge directories.
    const parts = name.split("/");
    for (let depth = 1; depth < parts.length; depth++) {
      const directory = parts.slice(0, depth).join("/");
      const key = directory.toLowerCase();
      if (directories.has(key) && directories.get(key) !== directory)
        return `分章目录大小写不一致：${directory}。请与已有目录使用相同大小写。`;
      directories.set(key, directory);
    }
    if (!source.trim() || source.length > 200000)
      return `分章 ${name} 不能为空，最多 200000 字符。`;
  }
  return "";
}

/** File selection is local; all source edits remain in the parent's revision-safe draft. */
export function MotionSourceEditor({
  engine,
  code,
  files,
  onCode,
  onFiles,
  onComposing,
  disabled,
}: {
  engine: MotionSpec["engine"];
  code: string;
  files: Record<string, string>;
  onCode: (code: string) => void;
  onFiles: (files: Record<string, string>) => void;
  onComposing: (value: boolean) => void;
  disabled: boolean;
}) {
  const entry = engine === "remotion-sdr-v1" ? "Animation.tsx" : "Animation.ts";
  const [selected, setSelected] = useState("");
  const active = Object.hasOwn(files, selected) ? selected : "";
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [removing, setRemoving] = useState("");
  return (
    <>
      <div className="frame-settings">
        <Field label="源码文件" id="motion-source-file">
          <select
            id="motion-source-file"
            value={active}
            disabled={disabled}
            onChange={(event) => setSelected(event.target.value)}
          >
            <option value="">{entry} · 入口</option>
            {Object.keys(files).map((path) => (
              <option key={path} value={path}>
                {path}
              </option>
            ))}
          </select>
        </Field>
        <div className="motion-save-actions">
          <Button
            disabled={disabled || Object.keys(files).length >= 128}
            onClick={() => {
              setName("");
              setError("");
              setAdding(true);
            }}
          >
            添加分章文件
          </Button>
          <Button
            disabled={disabled || !active}
            onClick={() => setRemoving(active)}
          >
            删除分章文件
          </Button>
        </div>
      </div>
      <p className="technical-note">
        入口通过相对路径导入分章，例如 import Chapter from
        "./chapters/intro"。由入口安排各章顺序与时间，共享画面规格并联合渲染；添加文件后请在入口中引用。
      </p>
      <CodeEditor
        key={active}
        value={active ? files[active] : code}
        onChange={(value) =>
          active ? onFiles({ ...files, [active]: value }) : onCode(value)
        }
        onComposing={onComposing}
      />
      <Modal
        open={adding}
        title="添加分章文件"
        onClose={() => setAdding(false)}
      >
        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            if (disabled) return;
            const path = name.trim();
            const source =
              "// 在入口导入此模块；使用帧号或时间计算状态，避免累积帧间变化。\nexport {};\n";
            const invalid = Object.keys(files).some(
              (item) => item.toLowerCase() === path.toLowerCase(),
            )
              ? "已有同名分章文件，请使用其他名称。"
              : sourceFilesError(code, { ...files, [path]: source });
            if (invalid) {
              setError(invalid);
              document.getElementById("motion-file-name")?.focus();
              return;
            }
            onFiles({ ...files, [path]: source });
            setSelected(path);
            setAdding(false);
          }}
        >
          <Field
            label="分章文件路径"
            id="motion-file-name"
            invalid={!!error}
            hint={
              error ||
              "例如 chapters/intro.ts 或 chapters/ending.tsx；支持 ts、tsx、js、jsx。"
            }
          >
            <input
              id="motion-file-name"
              autoFocus
              value={name}
              maxLength={180}
              spellCheck={false}
              aria-invalid={!!error}
              aria-describedby="motion-file-name-help"
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <div className="modal-actions">
            <Button onClick={() => setAdding(false)}>取消</Button>
            <Button type="submit" className="primary" disabled={disabled}>
              添加文件
            </Button>
          </div>
        </form>
      </Modal>
      <Modal
        open={!!removing}
        title="删除分章文件"
        onClose={() => setRemoving("")}
      >
        <p>
          从当前草稿删除 {removing}。请同时修改引用它的
          import；保存后生效，历史修订仍保留文件。
        </p>
        <div className="modal-actions">
          <Button autoFocus onClick={() => setRemoving("")}>
            保留文件
          </Button>
          <Button
            disabled={disabled}
            onClick={() => {
              const next = { ...files };
              delete next[removing];
              onFiles(next);
              setSelected("");
              setRemoving("");
            }}
          >
            删除文件
          </Button>
        </div>
      </Modal>
    </>
  );
}
