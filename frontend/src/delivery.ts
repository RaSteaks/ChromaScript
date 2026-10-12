import { useCallback, useEffect, useRef, useState } from "react";
import { call } from "./api";
import type { DeliveryConfiguration, DeliveryOptions } from "./types";

export const DEFAULT_DELIVERY: DeliveryConfiguration = {
  schema_version: 1,
  container: "mp4",
  codec: "h264",
  profile: "high",
  bit_depth: 8,
  sampling: "420",
  alpha_mode: "none",
  // The server resolves this gamut/transfer pair and its legacy combined alias.
  color_space: "Rec.709",
  gamma: "gamma2.4",
  color_output: "Rec.709",
  data_range: "limited",
  compression: null,
  quality_mode: "crf",
  crf: 18,
  bitrate_kbps: null,
  gop_seconds: 2,
};
export type DeliveryTarget = {
  id: string;
  configuration: DeliveryConfiguration;
  resolved: DeliveryOptions | null;
  pending: boolean;
  error: string;
  numbers: Partial<Record<"crf" | "bitrate_kbps" | "gop_seconds", string>>;
  numericInvalid: boolean;
};
const newTarget = (configuration = DEFAULT_DELIVERY): DeliveryTarget => ({
  id: crypto.randomUUID(),
  configuration: structuredClone(configuration),
  resolved: null,
  pending: true,
  error: "",
  numbers: {},
  numericInvalid: false,
});

/** Keep each target's draft through errors and ignore superseded capability responses. */
export function useDeliveryTargets() {
  const [targets, setTargets] = useState<DeliveryTarget[]>(() => [newTarget()]);
  const current = useRef(targets);
  const requests = useRef(new Map<string, AbortController>());
  const mounted = useRef(false);
  const publish = useCallback((next: DeliveryTarget[]) => {
    current.current = next;
    setTargets(next);
  }, []);
  const resolve = useCallback(
    async (
      id: string,
      configuration: DeliveryConfiguration,
      changed_field?: string,
      initial = false,
    ) => {
      requests.current.get(id)?.abort();
      const controller = new AbortController();
      requests.current.set(id, controller);
      publish(
        current.current.map((target) =>
          target.id === id
            ? {
                ...target,
                configuration,
                pending: true,
                error: "",
                numericInvalid: false,
                numbers: ["crf", "bitrate_kbps", "gop_seconds"].includes(
                  changed_field || "",
                )
                  ? target.numbers
                  : {},
              }
            : target,
        ),
      );
      try {
        const resolved = await call<DeliveryOptions>(
          "get_delivery_options",
          { configuration: initial ? undefined : configuration, changed_field },
          controller.signal,
        );
        if (
          !mounted.current ||
          requests.current.get(id) !== controller ||
          controller.signal.aborted
        )
          return;
        // A mixed legacy menu cannot represent the standardized color controls.
        // Keep the draft and block submission until the daemon exposes this contract.
        if (
          !resolved.options.color_space ||
          !resolved.options.gamma ||
          !resolved.color_encoding
        )
          throw new Error(
            "本地服务需要更新：请重新启动服务，再重试输出色彩设置。",
          );
        publish(
          current.current.map((target) =>
            target.id === id
              ? {
                  ...target,
                  configuration: resolved.configuration,
                  resolved,
                  pending: false,
                  error: "",
                  numbers: {},
                  numericInvalid: false,
                }
              : target,
          ),
        );
      } catch (error) {
        if (
          !mounted.current ||
          requests.current.get(id) !== controller ||
          controller.signal.aborted
        )
          return;
        publish(
          current.current.map((target) =>
            target.id === id
              ? {
                  ...target,
                  pending: false,
                  error:
                    error instanceof Error
                      ? error.message
                      : "交付选项更新失败，请重试。",
                }
              : target,
          ),
        );
      }
    },
    [publish],
  );

  useEffect(() => {
    mounted.current = true;
    for (const target of current.current)
      void resolve(target.id, target.configuration, undefined, true);
    const controllers = requests.current;
    return () => {
      mounted.current = false;
      for (const controller of controllers.values()) controller.abort();
    };
  }, [resolve]);

  function change<K extends keyof DeliveryConfiguration>(
    id: string,
    field: K,
    value: DeliveryConfiguration[K],
  ) {
    const target = current.current.find((item) => item.id === id);
    if (target)
      void resolve(id, { ...target.configuration, [field]: value }, field);
  }
  function changeNumber(
    id: string,
    field: "crf" | "bitrate_kbps" | "gop_seconds",
    raw: string,
  ) {
    const target = current.current.find((item) => item.id === id);
    const bounds = target?.resolved?.numeric_fields[field];
    if (!target || !bounds) return;
    const value = Number(raw);
    const invalid =
      !raw.trim() ||
      !Number.isFinite(value) ||
      value < bounds.min ||
      value > bounds.max ||
      (field !== "gop_seconds" && !Number.isInteger(value));
    requests.current.get(id)?.abort();
    requests.current.delete(id);
    publish(
      current.current.map((item) =>
        item.id === id
          ? {
              ...item,
              numbers: { ...item.numbers, [field]: raw },
              pending: false,
              error: "",
              numericInvalid: !!invalid,
            }
          : item,
      ),
    );
    if (!invalid)
      void resolve(id, { ...target.configuration, [field]: value }, field);
  }
  function retry(id: string) {
    const target = current.current.find((item) => item.id === id);
    if (target) void resolve(id, target.configuration);
  }
  function add() {
    if (current.current.length >= 20) return;
    const target = newTarget();
    publish([...current.current, target]);
    void resolve(target.id, target.configuration, undefined, true);
  }
  function remove(id: string) {
    if (current.current.length <= 1) return;
    requests.current.get(id)?.abort();
    requests.current.delete(id);
    const next = current.current.filter((target) => target.id !== id);
    publish(next);
    requestAnimationFrame(() =>
      document.getElementById(`delivery-${next[0].id}-container`)?.focus(),
    );
  }
  const configurations = targets.map((target) => target.configuration);
  const duplicate =
    new Set(configurations.map((value) => JSON.stringify(value))).size !==
    configurations.length;
  const ready =
    targets.every(
      (target) =>
        target.resolved &&
        !target.pending &&
        !target.error &&
        !target.numericInvalid,
    ) && !duplicate;
  return {
    targets,
    configurations,
    ready,
    duplicate,
    change,
    changeNumber,
    retry,
    add,
    remove,
  };
}
