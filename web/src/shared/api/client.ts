import { API_BASE } from "./base";
import { ApiError, toApiError } from "./error";
import { markRateLimited, observeRateLimitHeaders } from "./rate-limit";
import { getAccessToken, refreshOnce } from "./session";

export interface RequestOptions extends Omit<RequestInit, "body" | "headers"> {
  json?: unknown;
  auth?: boolean;
  retry?: boolean;
  headers?: HeadersInit;
  body?: BodyInit | null;
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { json, auth = true, retry = true, headers: rawHeaders, ...rest } = options;
  const headers = new Headers(rawHeaders);
  if (json !== undefined) headers.set("Content-Type", "application/json");
  headers.set("X-Request-Id", `web_${crypto.randomUUID()}`);

  const token = getAccessToken();
  if (auth && token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...rest,
      credentials: "include",
      headers,
      body: json !== undefined ? JSON.stringify(json) : options.body,
    });
  } catch {
    throw new ApiError({
      code: "NETWORK",
      message: "网络不可达，请先启动网关（127.0.0.1:8080）",
      status: 0,
    });
  }

  if (response.status === 401 && auth && retry && token) {
    const error = await toApiError(response);
    if (error.code === "TOKEN_EXPIRED" && (await refreshOnce())) {
      return apiRequest<T>(path, { ...options, retry: false });
    }
    throw error;
  }

  observeRateLimitHeaders(response.headers);
  if (!response.ok) {
    const error = await toApiError(response);
    if (error.status === 429) markRateLimited(error.retryAfter, error.message);
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
