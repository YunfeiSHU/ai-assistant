import { apiRequest } from "@/shared/api/client";
import type { Page } from "@/shared/api/types";

import type { Message, MessageFilters } from "./types";

export function listMessages(
  conversationId: string,
  filters: MessageFilters = {},
): Promise<Page<Message>> {
  const params = new URLSearchParams();
  params.set("limit", String(filters.limit ?? 30));
  params.set("order", filters.order ?? "desc");
  if (filters.cursor) params.set("cursor", filters.cursor);
  if (filters.after_seq !== undefined) params.set("after_seq", String(filters.after_seq));

  return apiRequest<Page<Message>>(
    `/conversations/${encodeURIComponent(conversationId)}/messages?${params.toString()}`,
  );
}

export function deleteMessage(id: string): Promise<void> {
  return apiRequest<void>(`/messages/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function regenerateMessage(id: string): Promise<{ user: Message; assistant: Message }> {
  return apiRequest(`/messages/${encodeURIComponent(id)}/regenerate`, { method: "POST" });
}
