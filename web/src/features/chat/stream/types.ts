import type {
  MessageReference,
  MessageUsage,
  ToolCallTrace,
} from "@/features/messages/types";

export interface SendMessageInput {
  content: string;
  use_rag: boolean;
  use_memory: boolean;
  use_tools: boolean;
  model?: string;
  attachment_doc_ids?: string[];
}

export interface StreamErrorInfo {
  code: string;
  message: string;
  retryable: boolean;
  traceId?: string;
}

export type ChatEvent =
  | {
      type: "meta";
      conversationId: string | null;
      messageId: string;
      userMessageId: string;
      userSeq: number;
      model: string;
      createdAt: string;
      degraded: boolean;
    }
  | { type: "references"; references: MessageReference[] }
  | { type: "token"; delta: string }
  | {
      type: "tool_call";
      callId: string;
      name: string;
      arguments: Record<string, unknown>;
    }
  | {
      type: "tool_result";
      callId: string;
      name: string;
      status: ToolCallTrace["status"];
      summary: string;
      elapsedMs: number;
    }
  | { type: "usage"; usage: MessageUsage }
  | {
      type: "done";
      finishReason: string;
      elapsedMs: number;
      partial: boolean;
    }
  | { type: "error"; code: string; message: string; retryable: boolean }
  | { type: "persist_error"; reason: string }
  | { type: "ping" }
  | { type: "unknown"; event: string; data: unknown };

export type StreamPhase =
  | "idle"
  | "connecting"
  | "streaming"
  | "done"
  | "aborted"
  | "error";

export interface TurnState {
  phase: StreamPhase;
  userText: string;
  userMessageId: string | null;
  userSeq: number | null;
  assistantId: string | null;
  model: string | null;
  content: string;
  references: MessageReference[];
  tools: ToolCallTrace[];
  usage: MessageUsage | null;
  finishReason: string | null;
  partial: boolean;
  elapsedMs: number | null;
  degraded: boolean;
  degradedReasons: string[];
  error: StreamErrorInfo | null;
  persistError: string | null;
  startedAt: number | null;
}
