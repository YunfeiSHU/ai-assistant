import { apiRequest } from "@/shared/api/client";
import type { Page } from "@/shared/api/types";

import type {
  Conversation,
  ConversationFilters,
  CreateConversationInput,
  UpdateConversationInput,
} from "./types";

function toQuery(filters: ConversationFilters): string {
  const params = new URLSearchParams();
  params.set("limit", String(filters.limit ?? 30));
  if (filters.cursor) params.set("cursor", filters.cursor);
  if (filters.status) params.set("status", filters.status);
  if (filters.keyword?.trim()) params.set("keyword", filters.keyword.trim());
  return `?${params.toString()}`;
}

export function listConversations(
  filters: ConversationFilters = {},
): Promise<Page<Conversation>> {
  return apiRequest<Page<Conversation>>(`/conversations${toQuery(filters)}`);
}

export interface MessageSearchHit {
  conversation_id: string;
  title: string;
  message_id: string;
  role: string;
  snippet: string;
  created_at: string;
}

export function searchMessages(
  query: string,
  limit = 20,
): Promise<{ items: MessageSearchHit[]; has_more: boolean }> {
  return apiRequest(
    `/search/messages?q=${encodeURIComponent(query)}&limit=${encodeURIComponent(String(limit))}`,
  );
}

export function getConversation(id: string): Promise<Conversation> {
  return apiRequest<Conversation>(`/conversations/${encodeURIComponent(id)}`);
}

export function createConversation(
  input: CreateConversationInput = {},
): Promise<Conversation> {
  return apiRequest<Conversation>("/conversations", {
    method: "POST",
    json: input,
  });
}

export function updateConversation(
  id: string,
  patch: UpdateConversationInput,
): Promise<Conversation> {
  return apiRequest<Conversation>(`/conversations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    json: patch,
  });
}

export function archiveConversation(id: string): Promise<Conversation> {
  return apiRequest<Conversation>(
    `/conversations/${encodeURIComponent(id)}/archive`,
    { method: "POST" },
  );
}

export function unarchiveConversation(id: string): Promise<Conversation> {
  return apiRequest<Conversation>(
    `/conversations/${encodeURIComponent(id)}/unarchive`,
    { method: "POST" },
  );
}

export function deleteConversation(id: string): Promise<void> {
  return apiRequest<void>(`/conversations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
