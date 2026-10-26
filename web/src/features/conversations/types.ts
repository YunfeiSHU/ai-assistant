export interface Conversation {
  id: string;
  user_id: string;
  title: string;
  title_source: string;
  status: "active" | "archived";
  model: string | null;
  kb_ids: string[];
  message_count: number;
  last_message_at: string | null;
  last_message_preview: string;
  pinned: boolean;
  metadata: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export interface CreateConversationInput {
  title?: string;
  model?: string;
  kb_ids?: string[];
}

export interface UpdateConversationInput {
  title?: string;
  pinned?: boolean;
  model?: string | null;
  kb_ids?: string[];
}

export interface ConversationFilters {
  status?: "active" | "archived";
  keyword?: string;
  limit?: number;
  cursor?: string | null;
}
