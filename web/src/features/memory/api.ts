import { apiRequest } from "@/shared/api/client";
import type { Page } from "@/shared/api/types";

import type {
  ConversationContext,
  ConversationSummary,
  Memory,
  MemoryCreateInput,
  MemorySettings,
  MemoryUpdateInput,
} from "./types";

export function listMemories({
  kind,
  limit = 20,
  cursor,
}: {
  kind?: string;
  limit?: number;
  cursor?: string | null;
} = {}): Promise<Page<Memory>> {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  if (kind) params.set("kind", kind);
  if (cursor) params.set("cursor", cursor);
  return apiRequest(`/memories?${params.toString()}`);
}

export function createMemory(input: MemoryCreateInput): Promise<Memory> {
  return apiRequest<Memory>("/memories", { method: "POST", json: input });
}

export function updateMemory(id: string, input: MemoryUpdateInput): Promise<Memory> {
  return apiRequest<Memory>(`/memories/${encodeURIComponent(id)}`, {
    method: "PATCH",
    json: input,
  });
}

export function deleteMemory(id: string): Promise<void> {
  return apiRequest<void>(`/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function clearMemories(): Promise<void> {
  return apiRequest<void>("/memories?all=true", { method: "DELETE" });
}

export function getMemorySettings(): Promise<MemorySettings> {
  return apiRequest<MemorySettings>("/memory-settings");
}

export function updateMemorySettings(input: {
  memory_enabled?: boolean;
  memory_top_n?: number;
}): Promise<MemorySettings> {
  return apiRequest<MemorySettings>("/memory-settings", { method: "PUT", json: input });
}

export function getConversationContext(conversationId: string): Promise<ConversationContext> {
  return apiRequest<ConversationContext>(
    `/conversations/${encodeURIComponent(conversationId)}/context`,
  );
}

export function deleteConversationContext(conversationId: string): Promise<void> {
  return apiRequest<void>(`/conversations/${encodeURIComponent(conversationId)}/context`, {
    method: "DELETE",
  });
}

export function getConversationSummary(conversationId: string): Promise<ConversationSummary> {
  return apiRequest<ConversationSummary>(
    `/conversations/${encodeURIComponent(conversationId)}/summary`,
  );
}

export function rebuildConversationSummary(
  conversationId: string,
): Promise<{ task_id: string; status: string }> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/summary/rebuild`, {
    method: "POST",
  });
}
