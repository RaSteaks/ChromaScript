/** Shared transport: tokens are in memory; mutations are never silently retried. */
let token = "";
/** Keep conflict codes available to editors while preserving existing Error messages. */
export class ApiError extends Error {
  constructor(
    message: string,
    public code: string = "request_failed",
  ) {
    super(message);
  }
}
export async function connect(
  signal?: AbortSignal,
): Promise<{ workspace: string }> {
  // Reconnecting notification streams bound session discovery as well as data reads.
  const response = await fetch("/api/session", { signal });
  if (!response.ok) throw new Error("本地服务未连接，请启动服务后重试。");
  const session = await response.json();
  token = session.token;
  return session;
}
export async function call<T>(
  operation: string,
  args: unknown = {},
  signal?: AbortSignal,
): Promise<T> {
  if (!token) await connect();
  const response = await fetch(
    "/api/operations/" + operation.replace("chromascript.", ""),
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + token,
      },
      body: JSON.stringify(args),
      signal,
    },
  );
  const data = await response.json();
  if (!response.ok) {
    // A previous daemon lets StaticFiles answer unknown POST routes with 405;
    // newer routers may use 404. Neither means the user's project data is empty.
    const unavailable = response.status === 404 || response.status === 405;
    const detail =
      typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg;
    throw new ApiError(
      unavailable
        ? "当前本地服务尚未提供此功能，请重启 ChromaScript 服务后重新连接。"
        : data.error?.message || detail || "操作未完成，请检查参数后重试。",
      unavailable ? "operation_unavailable" : data.error?.code,
    );
  }
  return data as T;
}
export function artifactUrl(id: string, download = false): string {
  return "/api/artifacts/" + id + (download ? "?download=1" : "");
}

/** Binary adapter of the same typed HDR operation; avoid Base64/JSON pixel copies. */
export async function encodeHDRPreview(
  header: unknown,
  frames: ArrayBuffer[],
  signal?: AbortSignal,
): Promise<unknown> {
  if (!token) await connect();
  const response = await fetch(
    "/api/operations/encode_motion_hdr_preview/binary",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        Authorization: "Bearer " + token,
        "X-Chroma-HDR": JSON.stringify(header),
      },
      body: new Blob(frames),
      signal,
    },
  );
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      data.error?.message || "HDR 预览编码失败",
      data.error?.code,
    );
  return data;
}
