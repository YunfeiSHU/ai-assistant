import { useCallback, useEffect, useReducer, useRef } from "react";

import { ApiError } from "@/shared/api/error";

import { initialTurn, streamReducer } from "./reducer";
import { streamChat } from "./stream";
import type { SendMessageInput, StreamErrorInfo } from "./types";

function toStreamError(error: unknown): StreamErrorInfo {
  if (error instanceof ApiError) {
    return {
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      traceId: error.traceId,
    };
  }
  if (error instanceof Error) {
    return { code: "STREAM_FAILED", message: error.message, retryable: true };
  }
  return { code: "STREAM_FAILED", message: "生成失败，请重试", retryable: true };
}

export function useChatStream(
  conversationId: string | undefined,
  onSettled?: () => Promise<void> | void,
) {
  const [turn, dispatch] = useReducer(streamReducer, initialTurn);

  const controllerRef = useRef<AbortController | null>(null);
  const bufferRef = useRef("");
  const timerRef = useRef<number | null>(null);
  const watchdogRef = useRef<number | null>(null);
  const lastEventAtRef = useRef(0);
  const abortedByUserRef = useRef(false);
  const timedOutRef = useRef(false);
  const terminalRef = useRef(false);
  const onSettledRef = useRef(onSettled);

  useEffect(() => {
    onSettledRef.current = onSettled;
  }, [onSettled]);

  const flush = useCallback(() => {
    if (!bufferRef.current) return;
    const delta = bufferRef.current;
    bufferRef.current = "";
    dispatch({ type: "token", delta });
  }, []);

  const scheduleFlush = useCallback(() => {
    if (timerRef.current !== null) return;
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      flush();
    }, 50);
  }, [flush]);

  const stop = useCallback(() => {
    if (!controllerRef.current) return;
    abortedByUserRef.current = true;
    dispatch({ type: "abort" });
    controllerRef.current.abort();
  }, []);

  const reset = useCallback(() => {
    dispatch({ type: "reset" });
  }, []);

  const send = useCallback(
    async (input: SendMessageInput) => {
      if (!conversationId || controllerRef.current) return;

      const controller = new AbortController();
      controllerRef.current = controller;
      bufferRef.current = "";
      terminalRef.current = false;
      abortedByUserRef.current = false;
      timedOutRef.current = false;
      lastEventAtRef.current = Date.now();
      dispatch({ type: "begin", userText: input.content });

      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }

      watchdogRef.current = window.setInterval(() => {
        if (Date.now() - lastEventAtRef.current <= 45_000) return;
        timedOutRef.current = true;
        dispatch({ type: "timeout" });
        controller.abort();
      }, 5_000);

      try {
        await streamChat({
          conversationId,
          input,
          signal: controller.signal,
          onEvent: (event) => {
            lastEventAtRef.current = Date.now();

            if (event.type === "token") {
              bufferRef.current += event.delta;
              scheduleFlush();
              return;
            }

            flush();
            if (event.type === "done" || event.type === "error") {
              terminalRef.current = true;
            }
            dispatch({ type: "event", event });
          },
        });
        if (!terminalRef.current && !abortedByUserRef.current && !timedOutRef.current) {
          flush();
          dispatch({
            type: "failure",
            error: {
              code: "STREAM_CLOSED",
              message: "连接已关闭，但没有收到 done 帧",
              retryable: true,
            },
          });
        }
      } catch (error) {
        flush();
        if (error instanceof DOMException && error.name === "AbortError") {
          if (timedOutRef.current) {
            // timeout 已在看门狗里 dispatch
          } else if (abortedByUserRef.current) {
            dispatch({ type: "abort" });
          } else {
            dispatch({
              type: "failure",
              error: {
                code: "STREAM_ABORTED",
                message: "连接被中断，已保留已生成内容",
                retryable: true,
              },
            });
          }
        } else {
          dispatch({ type: "failure", error: toStreamError(error) });
        }
      } finally {
        if (watchdogRef.current !== null) {
          window.clearInterval(watchdogRef.current);
          watchdogRef.current = null;
        }
        if (timerRef.current !== null) {
          window.clearTimeout(timerRef.current);
          timerRef.current = null;
          flush();
        }
        controllerRef.current = null;
        await onSettledRef.current?.();
      }
    },
    [conversationId, flush, scheduleFlush],
  );

  useEffect(
    () => () => {
      controllerRef.current?.abort();
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      if (watchdogRef.current !== null) window.clearInterval(watchdogRef.current);
    },
    [],
  );

  const isStreaming = turn.phase === "connecting" || turn.phase === "streaming";

  return { turn, send, stop, reset, isStreaming };
}
