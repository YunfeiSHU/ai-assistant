import type { ToolCallTrace } from "@/features/messages/types";

import type { ChatEvent, StreamErrorInfo, TurnState } from "./types";

export const initialTurn: TurnState = {
  phase: "idle",
  userText: "",
  userMessageId: null,
  userSeq: null,
  assistantId: null,
  model: null,
  content: "",
  references: [],
  tools: [],
  usage: null,
  finishReason: null,
  partial: false,
  elapsedMs: null,
  degraded: false,
  degradedReasons: [],
  error: null,
  persistError: null,
  startedAt: null,
};

export type StreamAction =
  | { type: "begin"; userText: string }
  | { type: "token"; delta: string }
  | { type: "event"; event: ChatEvent }
  | { type: "abort" }
  | { type: "timeout" }
  | { type: "failure"; error: StreamErrorInfo }
  | { type: "reset" };

function applyEvent(state: TurnState, event: ChatEvent): TurnState {
  switch (event.type) {
    case "meta":
      return {
        ...state,
        assistantId: event.messageId || state.assistantId,
        userMessageId: event.userMessageId || state.userMessageId,
        userSeq: event.userSeq > 0 ? event.userSeq : state.userSeq,
        model: event.model || state.model,
        degraded: event.degraded,
      };
    case "references":
      return { ...state, references: event.references };
    case "tool_call": {
      const tool: ToolCallTrace = {
        call_id: event.callId,
        name: event.name,
        arguments: event.arguments,
        status: "running",
        summary: "",
        elapsed_ms: 0,
      };
      // 正在执行的工具先占位；tool_result 到达后覆盖 status/summary。
      return { ...state, tools: [...state.tools, tool] };
    }
    case "tool_result": {
      const index = state.tools.findIndex((tool) => tool.call_id === event.callId);
      if (index < 0) {
        return {
          ...state,
          tools: [
            ...state.tools,
            {
              call_id: event.callId,
              name: event.name,
              arguments: {},
              status: event.status,
              summary: event.summary,
              elapsed_ms: event.elapsedMs,
            },
          ],
        };
      }
      const tools = state.tools.slice();
      tools[index] = {
        ...tools[index],
        status: event.status,
        summary: event.summary,
        elapsed_ms: event.elapsedMs,
      };
      return { ...state, tools };
    }
    case "usage":
      return { ...state, usage: event.usage };
    case "done":
      return {
        ...state,
        phase: "done",
        finishReason: event.finishReason,
        elapsedMs: event.elapsedMs,
        partial: event.partial,
      };
    case "error":
      return {
        ...state,
        phase: "error",
        error: {
          code: event.code,
          message: event.message,
          retryable: event.retryable,
        },
      };
    case "persist_error":
      return { ...state, persistError: event.reason };
    case "ping":
    case "unknown":
      return state;
    default:
      return state;
  }
}

export function streamReducer(state: TurnState, action: StreamAction): TurnState {
  switch (action.type) {
    case "begin":
      return {
        ...initialTurn,
        phase: "connecting",
        userText: action.userText,
        startedAt: Date.now(),
      };
    case "token":
      return {
        ...state,
        phase: "streaming",
        content: state.content + action.delta,
      };
    case "event":
      return applyEvent(state, action.event);
    case "abort":
      return state.phase === "done" || state.phase === "error"
        ? state
        : { ...state, phase: "aborted", partial: true };
    case "timeout":
      return {
        ...state,
        phase: "error",
        error: {
          code: "STREAM_IDLE_TIMEOUT",
          message: "超过 45 秒没有收到任何帧，已中止本次生成",
          retryable: true,
        },
      };
    case "failure":
      return { ...state, phase: "error", error: action.error };
    case "reset":
      return initialTurn;
    default:
      return state;
  }
}
