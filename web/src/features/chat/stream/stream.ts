import { createParser } from "eventsource-parser";

import { API_BASE } from "@/shared/api/base";
import { ApiError, toApiError } from "@/shared/api/error";
import { markRateLimited, observeRateLimitHeaders } from "@/shared/api/rate-limit";
import { getAccessToken, refreshOnce } from "@/shared/api/session";

import { parseFrame } from "./parse";
import type { ChatEvent, SendMessageInput } from "./types";

export interface StreamChatOptions {
  conversationId: string;
  input: SendMessageInput;
  signal: AbortSignal;
  onEvent: (event: ChatEvent) => void;
}

/**
 * 流式提问：fetch + ReadableStream。
 *
 * 为什么不用 EventSource：接口是 POST，且必须带 Authorization；EventSource 两者都不支持。
 */
export async function streamChat({
  conversationId,
  input,
  signal,
  onEvent,
}: StreamChatOptions): Promise<void> {
  const url = `${API_BASE}/conversations/${encodeURIComponent(conversationId)}/messages/stream`;

  const open = () => {
    const headers = new Headers({
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      "X-Request-Id": `web_${crypto.randomUUID()}`,
    });
    const token = getAccessToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);

    return fetch(url, {
      method: "POST",
      credentials: "include",
      headers,
      body: JSON.stringify(input),
      signal,
    });
  };

  let response = await open();
  observeRateLimitHeaders(response.headers);
  if (response.status === 401) {
    const error = await toApiError(response);
    // 只对 TOKEN_EXPIRED 刷新并重放一次；其它 401 直接抛。
    if (error.code === "TOKEN_EXPIRED" && (await refreshOnce())) {
      response = await open();
      observeRateLimitHeaders(response.headers);
    } else {
      throw error;
    }
  }

  if (!response.ok) {
    const error = await toApiError(response);
    if (error.status === 429) markRateLimited(error.retryAfter, error.message);
    throw error;
  }
  if (!response.body) {
    throw new ApiError({
      code: "UNEXPECTED_RESPONSE",
      message: "服务没有返回可读的流",
      status: response.status,
      retryable: true,
    });
  }

  const contentType = response.headers.get("Content-Type") ?? "";
  if (!contentType.includes("text/event-stream")) {
    throw new ApiError({
      code: "UNEXPECTED_RESPONSE",
      message: `响应不是 SSE：${contentType || "缺少 Content-Type"}`,
      status: response.status,
      retryable: false,
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = createParser({
    onEvent: (message) => {
      if (!message.event) return;
      onEvent(parseFrame(message.event, message.data));
    },
  });

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
  } finally {
    reader.releaseLock();
  }
}
