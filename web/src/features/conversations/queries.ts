import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  archiveConversation,
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  searchMessages,
  unarchiveConversation,
  updateConversation,
} from "./api";
import type {
  ConversationFilters,
  CreateConversationInput,
  UpdateConversationInput,
} from "./types";

export const conversationKeys = {
  all: ["conversations"] as const,
  list: (filters: ConversationFilters) =>
    [...conversationKeys.all, "list", filters] as const,
  detail: (id: string) => [...conversationKeys.all, "detail", id] as const,
  messages: (id: string) => [...conversationKeys.all, "messages", id] as const,
  search: (keyword: string) => [...conversationKeys.all, "search", keyword] as const,
};

export function useMessageSearch(keyword: string) {
  const query = keyword.trim();
  return useQuery({
    queryKey: conversationKeys.search(query),
    queryFn: () => searchMessages(query, 20),
    enabled: query.length > 0,
    staleTime: 5_000,
  });
}

export function useConversationList(filters: ConversationFilters = {}) {  return useInfiniteQuery({
    queryKey: conversationKeys.list(filters),
    queryFn: ({ pageParam }) =>
      listConversations({ ...filters, cursor: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 15_000,
  });
}

export function useConversation(id: string | undefined) {
  return useQuery({
    queryKey: conversationKeys.detail(id ?? ""),
    queryFn: () => getConversation(id as string),
    enabled: Boolean(id),
  });
}

export function useCreateConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateConversationInput = {}) => createConversation(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: conversationKeys.all });
    },
  });
}

export function useUpdateConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: UpdateConversationInput }) =>
      updateConversation(id, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: conversationKeys.all });
    },
  });
}

export function useArchiveConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, archived }: { id: string; archived: boolean }) =>
      archived ? archiveConversation(id) : unarchiveConversation(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: conversationKeys.all });
    },
  });
}

export function useDeleteConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: (_data, id) => {
      queryClient.removeQueries({ queryKey: conversationKeys.detail(id) });
      queryClient.removeQueries({ queryKey: conversationKeys.messages(id) });
      void queryClient.invalidateQueries({ queryKey: conversationKeys.all });
    },
  });
}
