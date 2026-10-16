import { useEffect, useId, useRef, useState } from "react";
import { ApiError, artifactUrl, call } from "./api";
import { Button, Field, Modal, Notice } from "./components";

type Asset = {
  id: string;
  name: string;
  duration_seconds: number;
  metadata?: { clipped_samples?: number };
};
type Clip = {
  asset_id: string;
  start_seconds: number;
  source_start_seconds: number;
  duration_seconds: number | null;
  gain_db: number;
};
type ClipDraft = {
  asset_id: string;
  start_seconds: string;
  source_start_seconds: string;
  duration_seconds: string;
  gain_db: string;
};
type Reply = {
  revision_id: string;
  assets: Asset[];
  timeline: { clips: Clip[] };
};
const toDraft = (clip: Clip): ClipDraft => ({
  asset_id: clip.asset_id,
  start_seconds: String(clip.start_seconds),
  source_start_seconds: String(clip.source_start_seconds),
  duration_seconds:
    clip.duration_seconds === null ? "" : String(clip.duration_seconds),
  gain_db: String(clip.gain_db),
});
const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

/** One editor for both workspaces. Explicit saves preserve revision conflicts;
 * closing dirty edits requires the shared app dialog, never a native confirm. */
export function ProjectAudio({
  projectId,
  disabled = false,
}: {
  projectId: string;
  disabled?: boolean;
}) {
  const prefix = useId();
  const [open, setOpen] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [reload, setReload] = useState(0);
  const [loaded, setLoaded] = useState<Reply | null>(null);
  const [clips, setClips] = useState<ClipDraft[]>([]);
  const [baseline, setBaseline] = useState("[]");
  const [pending, setPending] = useState("");
  const busy = useRef(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [status, setStatus] = useState("");
  const [name, setName] = useState("提示音");
  const [waveform, setWaveform] = useState("sine");
  const [frequency, setFrequency] = useState("440");
  const [duration, setDuration] = useState("1");
  const [invalid, setInvalid] = useState("");
  const file = useRef<HTMLInputElement>(null);
  const dirty = JSON.stringify(clips) !== baseline;

  useEffect(() => {
    if (!open) return;
    const abort = new AbortController();
    call<Reply>("get_project_audio", { project_id: projectId }, abort.signal)
      .then((reply) => {
        const next = reply.timeline.clips.map(toDraft);
        setLoaded(reply);
        setClips(next);
        setBaseline(JSON.stringify(next));
      })
      .catch((reason) => {
        if (!abort.signal.aborted) setError(message(reason));
      });
    return () => abort.abort();
  }, [open, projectId, reload]);

  useEffect(() => {
    if (!open || !dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [open, dirty]);

  // Focus after pending controls are re-enabled; focusing a disabled field during
  // validation is ignored by the browser and leaves keyboard users stranded.
  useEffect(() => {
    if (!pending && invalid && error) document.getElementById(invalid)?.focus();
  }, [pending, invalid, error]);

  async function act(label: string, work: () => Promise<void>) {
    if (busy.current) return;
    busy.current = true;
    setPending(label);
    setError("");
    setStatus("");
    try {
      await work();
    } catch (reason) {
      const changed =
        reason instanceof ApiError && reason.code === "revision_conflict";
      setConflict(changed);
      setError(
        changed
          ? "音轨已被其他客户端修改。草稿已保留；请复制草稿后重新读取最新音轨。"
          : message(reason),
      );
    } finally {
      busy.current = false;
      setPending("");
    }
  }

  function validate(id: string, text: string, min: number, max: number) {
    const value = Number(text);
    if (!text.trim() || !Number.isFinite(value) || value < min || value > max) {
      setInvalid(id);
      document.getElementById(id)?.focus();
      throw new Error(`请输入 ${min}–${max} 范围内的数值。`);
    }
    return value;
  }

  async function save() {
    if (!loaded) return;
    await act("正在保存音轨…", async () => {
      setInvalid("");
      const parsed = clips.map((clip, index) => {
        const asset = loaded.assets.find((a) => a.id === clip.asset_id);
        if (!asset) throw new Error("请选择有效音频素材。");
        const source = validate(
          `${prefix}-source-${index}`,
          clip.source_start_seconds,
          0,
          asset.duration_seconds,
        );
        const length = clip.duration_seconds.trim()
          ? validate(
              `${prefix}-length-${index}`,
              clip.duration_seconds,
              0.001,
              asset.duration_seconds - source,
            )
          : null;
        if (source >= asset.duration_seconds)
          throw new Error("素材起点必须小于素材时长。");
        return {
          asset_id: clip.asset_id,
          start_seconds: validate(
            `${prefix}-start-${index}`,
            clip.start_seconds,
            0,
            86400,
          ),
          source_start_seconds: source,
          duration_seconds: length,
          gain_db: validate(`${prefix}-gain-${index}`, clip.gain_db, -96, 12),
        };
      });
      const reply = await call<Omit<Reply, "assets">>("set_project_audio", {
        project_id: projectId,
        expected_revision_id: loaded.revision_id,
        timeline: { clips: parsed },
      });
      const next = reply.timeline.clips.map(toDraft);
      setLoaded({ ...loaded, ...reply });
      setClips(next);
      setBaseline(JSON.stringify(next));
      setConflict(false);
      setStatus("音轨已保存，将用于此项目的新交付任务。");
    });
  }

  function addAsset(asset: Asset) {
    setLoaded((current) =>
      current ? { ...current, assets: [asset, ...current.assets] } : current,
    );
    setStatus(
      asset.metadata?.clipped_samples
        ? "素材已创建，存在削波；建议降低生成音量。"
        : "音频素材已创建，可试听并添加到音轨。",
    );
  }

  function close() {
    if (busy.current) return;
    if (dirty) setDiscard(true);
    else setOpen(false);
  }

  const patch = (index: number, key: keyof ClipDraft, value: string) =>
    setClips((current) =>
      current.map((clip, i) =>
        i === index ? { ...clip, [key]: value } : clip,
      ),
    );
  const input = (
    label: string,
    id: string,
    value: string,
    onChange: (value: string) => void,
    hint?: string,
  ) => (
    <Field
      label={label}
      id={id}
      hint={invalid === id && error ? error : hint}
      invalid={invalid === id}
    >
      <input
        id={id}
        inputMode="decimal"
        value={value}
        aria-invalid={invalid === id}
        aria-describedby={
          hint || (invalid === id && error) ? id + "-help" : undefined
        }
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>
  );

  return (
    <>
      <Button
        className="outline"
        disabled={!projectId || disabled}
        onClick={() => {
          setLoaded(null);
          setError("");
          setStatus("");
          setConflict(false);
          setOpen(true);
        }}
      >
        项目音频
      </Button>
      <Modal open={open} title="项目音频" onClose={close}>
        {open && (
          <div className="project-audio">
            <p className="panel-description">
              音轨用于此项目的所有新交付任务。视频包含混音，图像序列附带
              WAV；保存音轨后，点击预览播放即可同步听见声音。
            </p>
            <div className="status-line" aria-live="polite">
              {pending || (!loaded && !error ? "正在读取音频…" : status)}
            </div>
            <Notice text={error} error />
            {!loaded && error && (
              <Button
                onClick={() => {
                  setError("");
                  setReload((n) => n + 1);
                }}
              >
                重新读取音频
              </Button>
            )}
            {loaded && (
              <fieldset disabled={!!pending} className="audio-editor-fields">
                <section aria-label="创建音频素材">
                  <h3>创建音频素材</h3>
                  <Field label="音频名称" id={prefix + "-name"}>
                    <input
                      id={prefix + "-name"}
                      value={name}
                      maxLength={120}
                      onChange={(e) => setName(e.target.value)}
                    />
                  </Field>
                  <div className="audio-fields">
                    <Field label="波形" id={prefix + "-wave"}>
                      <select
                        id={prefix + "-wave"}
                        value={waveform}
                        onChange={(e) => setWaveform(e.target.value)}
                      >
                        <option value="sine">正弦波</option>
                        <option value="triangle">三角波</option>
                        <option value="noise">噪声</option>
                      </select>
                    </Field>
                    {input(
                      "频率（Hz）",
                      prefix + "-frequency",
                      frequency,
                      setFrequency,
                    )}
                    {input(
                      "时长（秒）",
                      prefix + "-duration",
                      duration,
                      setDuration,
                    )}
                  </div>
                  <Button
                    onClick={() =>
                      void act("正在生成音频…", async () => {
                        if (!name.trim()) {
                          document.getElementById(prefix + "-name")?.focus();
                          throw new Error("请输入音频名称。");
                        }
                        const seconds = validate(
                          prefix + "-duration",
                          duration,
                          0.001,
                          600,
                        );
                        const hz = validate(
                          prefix + "-frequency",
                          frequency,
                          20,
                          20000,
                        );
                        const reply = await call<{ asset: Asset }>(
                          "create_audio",
                          {
                            project_id: projectId,
                            name,
                            spec: {
                              duration_seconds: seconds,
                              events: [
                                {
                                  waveform,
                                  duration_seconds: seconds,
                                  frequency_hz: hz,
                                  gain_db: -18,
                                },
                              ],
                            },
                          },
                        );
                        addAsset(reply.asset);
                      })
                    }
                  >
                    生成音频
                  </Button>
                  <Field
                    label="导入本地音频"
                    id={prefix + "-file"}
                    hint="单个文件最多 128 MiB、10 分钟。支持 WAV、MP3、FLAC、AIFF、OGG、M4A 等可解码音频。"
                  >
                    <input
                      ref={file}
                      id={prefix + "-file"}
                      type="file"
                      accept="audio/*,.m4a,.flac,.aiff"
                      aria-describedby={prefix + "-file-help"}
                    />
                  </Field>
                  <Button
                    onClick={() =>
                      void act("正在导入音频…", async () => {
                        const selected = file.current?.files?.[0];
                        if (!selected || selected.size > 128 * 1024 ** 2)
                          throw new Error("请选择不超过 128 MiB 的音频文件。");
                        const data = await new Promise<string>(
                          (resolve, reject) => {
                            const reader = new FileReader();
                            reader.onload = () =>
                              resolve(String(reader.result).split(",")[1]);
                            reader.onerror = () =>
                              reject(new Error("读取文件失败，请重新选择。"));
                            reader.readAsDataURL(selected);
                          },
                        );
                        const reply = await call<{ asset: Asset }>(
                          "import_audio",
                          {
                            project_id: projectId,
                            name: selected.name.slice(0, 120),
                            data_base64: data,
                          },
                        );
                        addAsset(reply.asset);
                        if (file.current) file.current.value = "";
                      })
                    }
                  >
                    导入音频
                  </Button>
                </section>
                <section aria-label="音频素材">
                  <h3>素材（{loaded.assets.length}）</h3>
                  {!loaded.assets.length ? (
                    <p>导入音频或生成音效，然后添加到音轨。</p>
                  ) : (
                    <AssetList
                      assets={loaded.assets}
                      full={clips.length >= 32}
                      onAdd={(asset) =>
                        setClips((current) => [
                          ...current,
                          {
                            asset_id: asset.id,
                            start_seconds: "0",
                            source_start_seconds: "0",
                            duration_seconds: "",
                            gain_db: "0",
                          },
                        ])
                      }
                    />
                  )}
                </section>
                <section aria-label="项目音轨">
                  <h3>
                    音轨（{clips.length}/32）{dirty ? " · 未保存" : ""}
                  </h3>
                  {!clips.length && <p>当前没有音轨，交付将保持静音。</p>}
                  {clips.map((clip, index) => (
                    <div className="audio-clip" key={index}>
                      <div className="section-heading">
                        <h4>
                          {index + 1}.{" "}
                          {loaded.assets.find((a) => a.id === clip.asset_id)
                            ?.name || "素材不可用"}
                        </h4>
                        <Button
                          aria-label={`移除音轨 ${index + 1}`}
                          onClick={() =>
                            setClips((current) =>
                              current.filter((_, i) => i !== index),
                            )
                          }
                        >
                          移除
                        </Button>
                      </div>
                      <div className="audio-fields">
                        {input(
                          "时间线起点（秒）",
                          `${prefix}-start-${index}`,
                          clip.start_seconds,
                          (v) => patch(index, "start_seconds", v),
                        )}
                        {input(
                          "素材起点（秒）",
                          `${prefix}-source-${index}`,
                          clip.source_start_seconds,
                          (v) => patch(index, "source_start_seconds", v),
                        )}
                        {input(
                          "播放时长（秒）",
                          `${prefix}-length-${index}`,
                          clip.duration_seconds,
                          (v) => patch(index, "duration_seconds", v),
                          "留空播放到素材末尾",
                        )}
                        {input(
                          "增益（dB）",
                          `${prefix}-gain-${index}`,
                          clip.gain_db,
                          (v) => patch(index, "gain_db", v),
                        )}
                      </div>
                    </div>
                  ))}
                </section>
                {conflict && (
                  <div className="modal-actions">
                    <Button
                      onClick={() =>
                        void act("正在复制草稿…", async () => {
                          await navigator.clipboard.writeText(
                            JSON.stringify(clips, null, 2),
                          );
                          setStatus("音轨草稿已复制。");
                        })
                      }
                    >
                      复制音轨草稿
                    </Button>
                    <Button onClick={() => setDiscard(true)}>
                      重新读取最新音轨
                    </Button>
                  </div>
                )}
                <div className="modal-actions">
                  <Button onClick={close}>关闭</Button>
                  <Button
                    className="primary"
                    disabled={!dirty || conflict}
                    onClick={() => void save()}
                  >
                    保存音轨
                  </Button>
                </div>
              </fieldset>
            )}
          </div>
        )}
      </Modal>
      <Modal
        open={discard}
        title="放弃音轨草稿？"
        onClose={() => setDiscard(false)}
      >
        <p>未保存的音轨编辑将丢失。已创建的音频素材会保留。</p>
        <div className="modal-actions">
          <Button onClick={() => setDiscard(false)}>继续编辑</Button>
          <Button
            onClick={() => {
              setDiscard(false);
              if (conflict) {
                setConflict(false);
                setError("");
                setLoaded(null);
                setReload((n) => n + 1);
              } else setOpen(false);
            }}
          >
            放弃草稿
          </Button>
        </div>
      </Modal>
    </>
  );
}

/** Bounded material list keeps long projects usable; native audio never autoplays. */
function AssetList({
  assets,
  full,
  onAdd,
}: {
  assets: Asset[];
  full: boolean;
  onAdd: (asset: Asset) => void;
}) {
  const [page, setPage] = useState(0);
  const index = Math.min(page, Math.max(0, Math.ceil(assets.length / 5) - 1));
  return (
    <>
      {assets.slice(index * 5, index * 5 + 5).map((asset) => (
        <div className="audio-asset" key={asset.id}>
          <div className="section-heading">
            <span>
              {asset.name} · {asset.duration_seconds.toFixed(2)} 秒
            </span>
            <Button
              disabled={full}
              aria-label={`添加 ${asset.name} 到音轨`}
              onClick={() => onAdd(asset)}
            >
              添加到音轨
            </Button>
          </div>
          <audio
            controls
            preload="none"
            aria-label={`试听 ${asset.name}`}
            src={artifactUrl(asset.id)}
          />
          <a href={artifactUrl(asset.id, true)}>下载 WAV</a>
        </div>
      ))}
      {assets.length > 5 && (
        <div className="modal-actions">
          <Button disabled={index === 0} onClick={() => setPage(index - 1)}>
            上一页素材
          </Button>
          <span>
            {index + 1} / {Math.ceil(assets.length / 5)}
          </span>
          <Button
            disabled={(index + 1) * 5 >= assets.length}
            onClick={() => setPage(index + 1)}
          >
            下一页素材
          </Button>
        </div>
      )}
    </>
  );
}
