export type MemoryKind = "preference" | "fact";

export interface Memory {
  id: string;
  content: string;
  kind: MemoryKind | string;
  confidence: number;
  hit_count: number;
  source_conversation_id: string | null;
  expires_at: string | null;
  expired: boolean;
  created_at: string;
  updated_at: string;
}

export interface MemorySettings {
  memory_enabled: boolean;
  memory_top_n: number;
  cleared_at: string | null;
}

export interface MemoryCreateInput {
  content: string;
  kind: MemoryKind;
  confidence?: number;
  expires_at?: string | null;
}

export interface MemoryUpdateInput {
  content?: string;
  kind?: MemoryKind;
  expires_at?: string | null;
}

export interface ContextMessage {
  role: string;
  message_id: string;
  content: string;
  tokens: number;
  created_at: string;
  partial: boolean;
}

export interface ContextSummary {
  exists: boolean;
  covered_until: string | null;
  token_count: number;
}

export interface ContextBudget {
  context_token_budget: number;
  used: Record<string, number>;
  total_tokens: number;
  trimmed: Record<string, number>;
}

export interface ConversationContext {
  conversation_id: string;
  message_count: number;
  messages: ContextMessage[];
  summary: ContextSummary;
  budget: ContextBudget;
}

export interface ConversationSummary {
  conversation_id: string;
  content: string;
  covered_until: string;
  source_message_count: number;
  token_count: number;
}
