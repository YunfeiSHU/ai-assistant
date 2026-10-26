export interface MessageUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface MessageReference {
  index: number;
  chunk_id: string;
  doc_id: string;
  kb_id: string;
  doc_name: string;
  page: number | null;
  heading_path: string | null;
  score: number;
  snippet: string;
  content_sha256: string;
}

export interface ToolCallTrace {
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  status: "running" | "ok" | "error" | "timeout" | "forbidden";
  summary: string;
  elapsed_ms: number;
}

export interface Message {
  id: string;
  conversation_id: string;
  user_id: string;
  seq: number;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  status: "completed" | "partial" | "failed";
  finish_reason: string | null;
  references: MessageReference[];
  tool_calls: ToolCallTrace[];
  usage: MessageUsage | null;
  model: string | null;
  degraded: boolean;
  degraded_reasons: string[];
  elapsed_ms: number | null;
  trace_id: string | null;
  created_at: string;
}

export interface MessageFilters {
  limit?: number;
  cursor?: string | null;
  order?: "asc" | "desc";
  after_seq?: number;
}
