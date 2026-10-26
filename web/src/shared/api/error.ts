export interface ApiErrorInit {
  code: string;
  message: string;
  status: number;
  details?: Record<string, unknown>;
  traceId?: string;
  retryable?: boolean;
  retryAfter?: number;
}

/** 与网关 `{"error": {...}}` 信封一一对应。 */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details?: Record<string, unknown>;
  readonly traceId?: string;
  readonly retryable: boolean;
  readonly retryAfter?: number;

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = "ApiError";
    this.code = init.code;
    this.status = init.status;
    this.details = init.details;
    this.traceId = init.traceId;
    this.retryable = init.retryable ?? false;
    this.retryAfter = init.retryAfter;
  }
}

interface ErrorEnvelope {
  error?: {
    code?: unknown;
    message?: unknown;
    details?: unknown;
    trace_id?: unknown;
    retryable?: unknown;
    retry_after?: unknown;
  };
}

function readRetryAfter(response: Response): number | undefined {
  const raw = response.headers.get("Retry-After");
  if (!raw) return undefined;
  const value = Number(raw);
  return Number.isFinite(value) ? value : undefined;
}

export async function toApiError(response: Response): Promise<ApiError> {
  const traceId = response.headers.get("X-Trace-Id") ?? undefined;
  const retryAfter = readRetryAfter(response);

  let envelope: ErrorEnvelope | null = null;
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    envelope = null;
  }

  const error = envelope?.error;
  if (error && typeof error.code === "string" && typeof error.message === "string") {
    return new ApiError({
      code: error.code,
      message: error.message,
      status: response.status,
      details:
        error.details && typeof error.details === "object"
          ? (error.details as Record<string, unknown>)
          : undefined,
      traceId: typeof error.trace_id === "string" ? error.trace_id : traceId,
      retryable: typeof error.retryable === "boolean" ? error.retryable : response.status >= 500,
      retryAfter:
        typeof error.retry_after === "number" ? error.retry_after : retryAfter,
    });
  }

  return new ApiError({
    code: "UNEXPECTED_RESPONSE",
    message: `服务返回了非约定格式（HTTP ${response.status}）`,
    status: response.status,
    traceId,
    retryable: response.status >= 500,
    retryAfter,
  });
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.code} · ${error.message}`;
  return "请求失败，请稍后重试";
}
