/** Shared transport: tokens are in memory; mutations are never silently retried. */
let token = "";
export async function connect(): Promise<{ workspace: string }> {
  const response = await fetch("/api/session");
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
  if (!response.ok)
    throw new Error(
      data.error?.message ||
        data.detail?.[0]?.msg ||
        "操作未完成，请检查参数后重试。",
    );
  return data as T;
}
export function artifactUrl(id: string, download = false): string {
  return "/api/artifacts/" + id + (download ? "?download=1" : "");
}
