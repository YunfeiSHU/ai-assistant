import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import type { Page } from "@/shared/api/types";

import { deleteMessage, listMessages, regenerateMessage } from "./api";
import type { Message } from "./types";

export const messageKeys = {
  list: (conversationId: string) =>
    ["conversations", conversationId, "messages"] as const,
};

export function useMessages(conversationId: string | undefined) {
  return useInfiniteQuery({
    queryKey: messageKeys.list(conversationId ?? ""),
    queryFn: ({ pageParam }) =>
      listMessages(conversationId as string, {
        cursor: pageParam,
        limit: 30,
        order: "desc",
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(conversationId),
    staleTime: 10_000,
  });
}

export function useDeleteMessage(_conversationId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (messageId: string) => deleteMessage(messageId),
    onSuccess: () => {
      // 前缀覆盖 detail / messages / context / summary / list。
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

export function useRegenerateMessage(_conversationId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (messageId: string) => regenerateMessage(messageId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/**
 * B-11：用 after_seq 只拉新增消息并合并到第一页（页面按 seq 降序）。
 * 返回新增条数；0 表示已是最新。
 */
export function useSyncAfterSeq(conversationId: string | undefined) {
  const queryClient = useQueryClient();
  return useCallback(async (): Promise<number> => {
    if (!conversationId) return 0;
    const key = messageKeys.list(conversationId);
    const old = queryClient.getQueryData<{
      pages: Page<Message>[];
      pageParams: unknown[];
    }>(key);
    if (!old) return 0;
    const latestSeq = old.pages.reduce(
      (max, page) => Math.max(max, ...page.items.map((item) => item.seq), 0),
      0,
    );
    if (latestSeq <= 0) return 0;
    const page = await listMessages(conversationId, {
      after_seq: latestSeq,
      order: "asc",
      limit: 100,
    });
    if (!page.items.length) return 0;

    queryClient.setQueryData(key, (current: typeof old) => {
      if (!current?.pages.length) return current;
      const seen = new Set(current.pages.flatMap((p) => p.items.map((m) => m.id)));
      const added = page.items.filter((item) => !seen.has(item.id));
      if (!added.length) return current;
      const first = current.pages[0];
      const merged = [...added, ...first.items].sort((a, b) => b.seq - a.seq);
      const pages = current.pages.slice();
      pages[0] = { ...first, items: merged };
      return { ...current, pages };
    });
    void queryClient.invalidateQueries({ queryKey: ["conversations", conversationId] });
    return page.items.length;
  }, [conversationId, queryClient]);
}
