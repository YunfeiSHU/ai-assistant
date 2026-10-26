import type { MessageReference, MessageUsage } from "@/features/messages/types";

import type { ChatEvent } from "./types";

function asRecord(data: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(data);
    return value !== null && typeof value === "object"
      ? (value as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function bool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function parseReferences(value: unknown): MessageReference[] {
  return Array.isArray(value) ? (value as MessageReference[]) : [];
}

function parseUsage(value: unknown): MessageUsage {
  const raw = value !== null && typeof value === "object" ? (value as Record<string, unknown>) : {};
  return {
    prompt_tokens: num(raw.prompt_tokens),
    completion_tokens: num(raw.completion_tokens),
    total_tokens: num(raw.total_tokens),
  };
}

export function parseFrame(event: string, data: string): ChatEvent {
  const raw = asRecord(data);

  switch (event) {
    case "meta":
      return {
        type: "meta",
        conversationId: typeof raw.conversation_id === "string" ? raw.conversation_id : null,
        messageId: str(raw.message_id),
        userMessageId: str(raw.user_message_id),
        userSeq: typeof raw.user_seq === "number" ? raw.user_seq : 0,
        model: str(raw.model),
        createdAt: str(raw.created_at),
        degraded: bool(raw.degraded),
      };
    case "reference":
      return { type: "references", references: parseReferences(raw.references) };
    case "token":
      return { type: "token", delta: str(raw.delta) };
    case "tool_call":
      return {
        type: "tool_call",
        callId: str(raw.call_id),
        name: str(raw.name),
        arguments:
          raw.arguments !== null && typeof raw.arguments === "object"
            ? (raw.arguments as Record<string, unknown>)
            : {},
      };
    case "tool_result": {
      const status = str(raw.status);
      return {
        type: "tool_result",
        callId: str(raw.call_id),
        name: str(raw.name),
        status:
          status === "ok" || status === "error" || status === "timeout" || status === "forbidden"
            ? status
            : "error",
        summary: str(raw.summary),
        elapsedMs: num(raw.elapsed_ms),
      };
    }
    case "usage":
      return { type: "usage", usage: parseUsage(raw) };
    case "done":
      return {
        type: "done",
        finishReason: str(raw.finish_reason, "stop"),
        elapsedMs: num(raw.elapsed_ms),
        partial: bool(raw.partial),
      };
    case "error":
      return {
        type: "error",
        code: str(raw.code, "STREAM_ERROR"),
        message: str(raw.message, "生成失败"),
        retryable: bool(raw.retryable),
      };
    case "gw_persist_error":
      return { type: "persist_error", reason: str(raw.reason, "persist_failed") };
    case "ping":
      return { type: "ping" };
    default:
      return { type: "unknown", event, data: raw };
  }
}
