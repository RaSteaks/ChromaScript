import { useEffect, useId, useRef, useState } from "react";
import { call } from "./api";
import { Button, Field, Modal, Notice, Textarea } from "./components";
import type { AudioAsset } from "./ProjectAudio";

type Provider = "openai" | "openrouter";
type Configuration = {
  provider: string;
  api_key_configured: boolean;
  configuration_error?: string;
  model?: string;
  voice?: string;
  supported_voices: string[];
  supported_formats?: string[];
  env_files: string[];
  privacy_policy?: { zdr?: boolean };
};
type Generation = {
  project_id: string;
  name: string;
  idempotency_key: string;
  request: {
    provider: Provider;
    text: string;
    instructions: string;
    model: string;
    voice: string;
    response_format: "pcm" | "mp3";
  };
};
type Generated = { asset: AudioAsset; request_id: string };
type RequestStatus =
  | (Generated & { status: "complete" })
  | { request_id: string; status: "pending" | "failed" | "not_found" };
type Confirmation = { args: Generation; snapshot: string };
type Attempt = Confirmation & { complete: boolean };
const providerName = (provider: Provider) =>
  provider === "openai" ? "OpenAI" : "OpenRouter";
const describe = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

/** Drafts live with the retained project editor, never in browser storage.
 * Recovery reads durable status; it cannot dispatch another paid request. */
export function SpeechGenerator({
  projectId,
  active,
  disabled,
  onCreated,
  onDirty,
  onPending,
}: {
  projectId: string;
  active: boolean;
  disabled: boolean;
  onCreated: (asset: AudioAsset) => void;
  onDirty: (dirty: boolean) => void;
  onPending: (pending: boolean) => void;
}) {
  const prefix = useId();
  const [provider, setProvider] = useState<Provider | "">("");
  const [name, setName] = useState("旁白");
  const [text, setText] = useState("");
  const [instructions, setInstructions] = useState("");
  const [voices, setVoices] = useState<Partial<Record<Provider, string>>>({});
  const [format, setFormat] = useState<"pcm" | "mp3">("pcm");
  const [configs, setConfigs] = useState<
    Partial<Record<Provider, Configuration>>
  >({});
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [invalid, setInvalid] = useState("");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [pending, setPending] = useState<"generate" | "check" | "">("");
  const [baseline, setBaseline] = useState("");
  const busy = useRef(false);
  const config = provider ? configs[provider] : undefined;
  const voice = provider ? (voices[provider] ?? config?.voice ?? "") : "";
  const responseFormat =
    provider === "openrouter" && config?.supported_formats?.includes(format)
      ? format
      : "pcm";
  const snapshot = JSON.stringify({
    provider,
    name,
    text,
    instructions,
    voice,
    model: config?.model,
    responseFormat,
  });
  const dirty =
    (!!text || !!instructions || name !== "旁白") && snapshot !== baseline;

  useEffect(
    () => onDirty(dirty || !!confirmation || (!!attempt && !attempt.complete)),
    [dirty, confirmation, attempt, onDirty],
  );
  useEffect(() => onPending(!!pending), [pending, onPending]);

  useEffect(() => {
    if (!provider || !active) return;
    const abort = new AbortController();
    setLoading(true);
    setLoadError("");
    void call<Configuration>("get_speech_config", { provider }, abort.signal)
      .then((reply) => {
        if (!abort.signal.aborted)
          setConfigs((old) => ({ ...old, [provider]: reply }));
      })
      .catch((reason) => {
        if (!abort.signal.aborted) setLoadError(describe(reason));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [provider, reload, active]);

  function prepare() {
    if (busy.current || confirmation) return;
    setError("");
    setInvalid("");
    const issue = !provider
      ? ["provider", "请选择配音供应商。"]
      : !name.trim()
        ? ["name", "请输入配音名称。"]
        : !text.trim()
          ? ["text", "请输入需要朗读的台词。"]
          : !voice.trim()
            ? ["voice", "请选择或填写配音音色。"]
            : null;
    if (issue) {
      setInvalid(issue[0]);
      setError(issue[1]);
      document.getElementById(`${prefix}-${issue[0]}`)?.focus();
      return;
    }
    if (
      !provider ||
      !config?.model ||
      !config.api_key_configured ||
      config.configuration_error ||
      loading ||
      loadError
    ) {
      setError("请先完成本地密钥配置并重新读取配音配置。");
      return;
    }
    setConfirmation({
      snapshot,
      args: {
        project_id: projectId,
        name,
        idempotency_key: crypto.randomUUID(),
        request: {
          provider,
          text,
          instructions,
          model: config.model,
          voice,
          response_format: responseFormat,
        },
      },
    });
  }

  async function perform(confirmed?: Confirmation) {
    const current = confirmed ?? attempt;
    if (!current || busy.current) return;
    busy.current = true;
    setPending(confirmed ? "generate" : "check");
    setConfirmation(null);
    setError("");
    setNotice("");
    if (confirmed) setAttempt({ ...confirmed, complete: false });
    try {
      const reply: RequestStatus = confirmed
        ? {
            status: "complete",
            ...(await call<Generated>("generate_speech", confirmed.args)),
          }
        : await call<RequestStatus>("get_speech_request", {
            project_id: current.args.project_id,
            idempotency_key: current.args.idempotency_key,
          });
      if (reply.status === "complete") {
        onCreated(reply.asset);
        setAttempt({ ...current, complete: true });
        setBaseline(current.snapshot);
        setNotice("AI 生成配音已加入素材，可试听并添加到音轨。");
      } else {
        setNotice(
          {
            pending: "本次请求仍在处理中。可稍后再次检查结果，不会重新生成。",
            failed:
              "本次生成未完成，已收到的源文件会保留。检查结果不会重试；如需新一版，请明确发起生成，可能再次计费。",
            not_found:
              "服务端未找到本次请求记录。如需生成，请重新发起并确认费用。",
          }[reply.status],
        );
      }
    } catch (reason) {
      setError(describe(reason));
      if (confirmed)
        setNotice(
          "本次生成尚未确认完成，可能已产生费用。请先检查本次结果；系统不会自动重新生成。",
        );
    } finally {
      busy.current = false;
      setPending("");
    }
  }

  const help = (field: string) => (invalid === field ? error : undefined);
  const describedBy = (field: string) =>
    invalid === field ? `${prefix}-${field}-help` : undefined;

  return (
    <section className="speech-generator" aria-label="云端配音">
      <h3>云端配音</h3>
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          prepare();
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" && event.nativeEvent.isComposing)
            event.preventDefault();
        }}
      >
        <fieldset
          className="audio-editor-fields"
          disabled={disabled || !!pending}
        >
          <div className="audio-fields">
            <Field
              label="配音供应商"
              id={`${prefix}-provider`}
              hint={help("provider")}
              invalid={invalid === "provider"}
            >
              <select
                id={`${prefix}-provider`}
                value={provider}
                aria-invalid={invalid === "provider"}
                aria-describedby={describedBy("provider")}
                onChange={(event) => {
                  const value = event.target.value;
                  if (
                    value === "openai" ||
                    value === "openrouter" ||
                    value === ""
                  )
                    setProvider(value);
                  setInvalid("");
                  setError("");
                }}
              >
                <option value="">请选择</option>
                <option value="openai">OpenAI</option>
                <option value="openrouter">OpenRouter</option>
              </select>
            </Field>
            <Field
              label="配音名称"
              id={`${prefix}-name`}
              hint={help("name")}
              invalid={invalid === "name"}
            >
              <input
                id={`${prefix}-name`}
                value={name}
                maxLength={120}
                onChange={(event) => setName(event.target.value)}
                aria-invalid={invalid === "name"}
                aria-describedby={describedBy("name")}
              />
            </Field>
          </div>
          {provider && (
            <div className="speech-configuration">
              <div className="status-line" role="status">
                {loading
                  ? "正在读取配音配置…"
                  : loadError || config?.configuration_error
                    ? "配音配置读取失败"
                    : config?.api_key_configured
                      ? "密钥已配置，尚未验证"
                      : "密钥未配置"}
              </div>
              <Notice
                text={
                  loadError ||
                  (config?.configuration_error
                    ? "无法读取本地配音配置，请检查 .env 权限和内容。"
                    : "")
                }
                error
              />
              <p className="panel-description">
                在本地 .env 或服务进程环境中设置{" "}
                <code>
                  {provider === "openai"
                    ? "OPENAI_API_KEY"
                    : "OPENROUTER_API_KEY"}
                </code>
                ，文件权限需为 600。密钥只由后端读取。
              </p>
              {config?.env_files.map((path) => (
                <div className="mono speech-config-path" key={path}>
                  {path}
                </div>
              ))}
              <Button
                disabled={loading}
                onClick={() => setReload((value) => value + 1)}
              >
                重新读取配音配置
              </Button>
            </div>
          )}
          <Field
            label="配音台词"
            id={`${prefix}-text`}
            hint={
              help("text") ||
              "最多 12000 字符，按原文朗读；生成结果仍需试听核对。"
            }
            invalid={invalid === "text"}
          >
            <Textarea
              className="resize-none"
              id={`${prefix}-text`}
              rows={6}
              value={text}
              maxLength={12000}
              onChange={(event) => setText(event.target.value)}
              aria-invalid={invalid === "text"}
              aria-describedby={`${prefix}-text-help`}
            />
          </Field>
          <Field
            label="演绎要求"
            id={`${prefix}-instructions`}
            hint="可填写语言、语气、情绪和语速，最多 4000 字符。"
          >
            <Textarea
              className="resize-none"
              id={`${prefix}-instructions`}
              rows={3}
              value={instructions}
              maxLength={4000}
              onChange={(event) => setInstructions(event.target.value)}
              aria-describedby={`${prefix}-instructions-help`}
            />
          </Field>
          {provider && (
            <div className="audio-fields">
              <Field label="配音模型" id={`${prefix}-model`}>
                <input
                  id={`${prefix}-model`}
                  value={config?.model ?? ""}
                  readOnly
                />
              </Field>
              <Field
                label="配音音色"
                id={`${prefix}-voice`}
                hint={help("voice")}
                invalid={invalid === "voice"}
              >
                {provider === "openai" ? (
                  <select
                    id={`${prefix}-voice`}
                    value={voice}
                    aria-invalid={invalid === "voice"}
                    aria-describedby={describedBy("voice")}
                    onChange={(event) =>
                      setVoices((old) => ({
                        ...old,
                        openai: event.target.value,
                      }))
                    }
                  >
                    {!config && <option value="">等待配置</option>}
                    {config?.supported_voices.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    id={`${prefix}-voice`}
                    value={voice}
                    maxLength={200}
                    aria-invalid={invalid === "voice"}
                    aria-describedby={describedBy("voice")}
                    onChange={(event) =>
                      setVoices((old) => ({
                        ...old,
                        openrouter: event.target.value,
                      }))
                    }
                  />
                )}
              </Field>
              {provider === "openrouter" &&
                (config?.supported_formats?.length ?? 0) > 1 && (
                  <Field label="配音源格式" id={`${prefix}-format`}>
                    <select
                      id={`${prefix}-format`}
                      value={responseFormat}
                      onChange={(event) =>
                        setFormat(event.target.value === "mp3" ? "mp3" : "pcm")
                      }
                    >
                      {config?.supported_formats?.map((value) => (
                        <option key={value} value={value}>
                          {value.toUpperCase()}
                        </option>
                      ))}
                    </select>
                  </Field>
                )}
            </div>
          )}
          {provider && (
            <p className="panel-description">
              {provider === "openai"
                ? "台词与演绎要求会发送至 OpenAI，可能产生费用。数据留存由 OpenAI 账户设置控制，本应用未验证零数据留存。"
                : `台词与演绎要求会发送至 OpenRouter 及其提供商，可能产生费用。${config?.privacy_policy?.zdr === false ? "本地配置已关闭 ZDR 要求。" : "请求要求零数据留存（ZDR），这不是独立隐私认证。"}`}
            </p>
          )}
          <Button
            type="submit"
            className="primary"
            disabled={
              !!provider &&
              (loading ||
                !!loadError ||
                !!config?.configuration_error ||
                !config?.api_key_configured)
            }
          >
            {attempt ? "生成新一版配音" : "生成配音"}
          </Button>
        </fieldset>
      </form>
      <div className="status-line" role="status">
        {pending === "generate"
          ? "正在生成配音，请等待…"
          : pending === "check"
            ? "正在检查本次结果…"
            : ""}
      </div>
      <Notice text={error} error />
      <Notice text={notice} />
      {attempt && (
        <div className="speech-attempt">
          <p>
            {providerName(attempt.args.request.provider)} · {attempt.args.name}
          </p>
          <p className="mono speech-config-path">
            请求标识：{attempt.args.idempotency_key}
          </p>
          {!attempt.complete && (
            <Button
              disabled={!!pending || disabled}
              onClick={() => void perform()}
            >
              检查本次结果
            </Button>
          )}
        </div>
      )}
      <Modal
        open={!!confirmation}
        title="确认生成云端配音"
        onClose={() => setConfirmation(null)}
      >
        {confirmation && (
          <>
            <p>
              将使用 {providerName(confirmation.args.request.provider)} 为“
              {confirmation.args.name}”生成 AI
              配音，台词与演绎要求会发送至该供应商，可能产生费用。
            </p>
            <p>
              模型：{confirmation.args.request.model}；音色：
              {confirmation.args.request.voice}。
            </p>
            {confirmation.args.request.provider === "openai" && (
              <p>数据留存由 OpenAI 账户设置控制，本应用未验证零数据留存。</p>
            )}
            {attempt && !attempt.complete && (
              <Notice text="上一请求尚未确认完成。新一版会使用新的请求标识，可能再次计费。" />
            )}
            <div className="modal-actions">
              <Button onClick={() => setConfirmation(null)}>取消</Button>
              <Button
                className="primary"
                onClick={() => void perform(confirmation)}
              >
                确认生成并可能付费
              </Button>
            </div>
          </>
        )}
      </Modal>
    </section>
  );
}
