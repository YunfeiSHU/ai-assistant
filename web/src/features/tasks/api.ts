import { API_BASE } from "@/shared/api/base";
import { apiRequest } from "@/shared/api/client";
import { getAccessToken } from "@/shared/api/session";

import type { Task, TaskFilters } from "./types";

export function listTasks(
  filters: TaskFilters = {},
): Promise<{ items: Task[]; next_cursor: string | null; has_more: boolean }> {
  const params = new URLSearchParams();
  params.set("limit", String(filters.limit ?? 30));
  if (filters.cursor) params.set("cursor", filters.cursor);
  if (filters.status) params.append("status", filters.status);
  if (filters.type) params.set("type", filters.type);
  if (filters.resource_id) params.set("resource_id", filters.resource_id);
  return apiRequest(`/tasks?${params.toString()}`);
}

export function getTask(id: string): Promise<Task> {
  return apiRequest(`/tasks/${encodeURIComponent(id)}`);
}

export function cancelTask(id: string): Promise<Task> {
  return apiRequest(`/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

export function retryTask(id: string): Promise<Task> {
  return apiRequest(`/tasks/${encodeURIComponent(id)}/retry`, { method: "POST" });
}

export interface TaskStreamEvent {
  event: string;
  data: unknown;
}

/**
 * B-01：任务进度 SSE（fetch 而非 EventSource，因为要带 Bearer）。
 * 流正常/异常结束都由调用方决定是否回退轮询；本函数不做重连。
 */
export async function streamTaskEvents(
  id: string,
  onEvent: (event: TaskStreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const headers = new Headers({ Accept: "text/event-stream" });
  const token = getAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}/tasks/${encodeURIComponent(id)}/events`, {
    headers,
    credentials: "include",
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`task event stream failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7).trim();
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) {
        try {
          onEvent({ event, data: JSON.parse(data) });
        } catch {
          onEvent({ event, data });
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
}
