import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  clearMemories,
  createMemory,
  deleteConversationContext,
  deleteMemory,
  getConversationContext,
  getConversationSummary,
  getMemorySettings,
  listMemories,
  rebuildConversationSummary,
  updateMemory,
  updateMemorySettings,
} from "./api";
import type { MemoryCreateInput, MemoryUpdateInput } from "./types";

export const memoryKeys = {
  all: ["memories"] as const,
  list: (kind: string) => [...memoryKeys.all, "list", kind] as const,
  settings: ["memory-settings"] as const,
  context: (conversationId: string) =>
    ["conversations", conversationId, "context"] as const,
  summary: (conversationId: string) =>
    ["conversations", conversationId, "summary"] as const,
};

export function useMemories(kind = "", enabled = true) {
  return useInfiniteQuery({
    queryKey: memoryKeys.list(kind),
    queryFn: ({ pageParam }) =>
      listMemories({ kind: kind || undefined, cursor: pageParam, limit: 20 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled,
    staleTime: 5_000,
  });
}

export function useMemorySettings() {
  return useQuery({
    queryKey: memoryKeys.settings,
    queryFn: getMemorySettings,
    staleTime: 10_000,
  });
}

export function useCreateMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: MemoryCreateInput) => createMemory(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });
}

export function useUpdateMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: MemoryUpdateInput }) =>
      updateMemory(id, input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });
}

export function useDeleteMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteMemory(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });
}

export function useClearMemories() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: clearMemories,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });
}

export function useUpdateMemorySettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { memory_enabled?: boolean; memory_top_n?: number }) =>
      updateMemorySettings(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: memoryKeys.settings });
      void queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });
}

export function useConversationContext(conversationId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: memoryKeys.context(conversationId ?? ""),
    queryFn: () => getConversationContext(conversationId as string),
    enabled: Boolean(conversationId) && enabled,
  });
}

export function useConversationSummary(conversationId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: memoryKeys.summary(conversationId ?? ""),
    queryFn: () => getConversationSummary(conversationId as string),
    enabled: Boolean(conversationId) && enabled,
    retry: false,
  });
}

export function useClearConversationContext(conversationId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => deleteConversationContext(conversationId as string),
    onSuccess: () => {
      if (conversationId) {
        void queryClient.invalidateQueries({ queryKey: memoryKeys.context(conversationId) });
        void queryClient.invalidateQueries({ queryKey: memoryKeys.summary(conversationId) });
      }
    },
  });
}

export function useRebuildConversationSummary(conversationId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => rebuildConversationSummary(conversationId as string),
    onSuccess: () => {
      if (conversationId) {
        void queryClient.invalidateQueries({ queryKey: memoryKeys.context(conversationId) });
        void queryClient.invalidateQueries({ queryKey: memoryKeys.summary(conversationId) });
      }
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}
