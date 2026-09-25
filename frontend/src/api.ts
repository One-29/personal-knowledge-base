const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail || `请求失败（${status}）`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function errorDetail(body: unknown, fallback: string): string {
  if (!isRecord(body) || !("detail" in body)) return fallback;
  if (typeof body.detail === "string") return body.detail;
  const serialized = JSON.stringify(body.detail);
  return typeof serialized === "string" && serialized.length > 0 ? serialized : fallback;
}

export async function apiResponse(path: string, options: RequestInit = {}): Promise<Response> {
  let response: Response;
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  try {
    response = await fetch(API_PREFIX + path, { ...options, headers });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new ApiError(0, "无法连接本机服务，请确认启动窗口仍在运行");
  }

  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = errorDetail(await response.json(), detail);
    } catch {
      // 非 JSON 错误响应沿用 HTTP 状态文本。
    }
    throw new ApiError(response.status, detail);
  }
  return response;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await apiResponse(path, options);
  if (response.status === 204) return undefined as T;
  return await response.json() as T;
}
