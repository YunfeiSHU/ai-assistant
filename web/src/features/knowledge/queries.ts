import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import type { TaskFilters } from "@/features/tasks/types";

import {
  createKnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  listChunks,
  listDocuments,
  listKnowledgeBases,
  searchKnowledgeBase,
  updateKnowledgeBase,
  uploadDocument,
} from "./api";
import type {
  DocumentFilters,
  KnowledgeBaseFilters,
  KnowledgeBaseInput,
  SearchRequest,
} from "./types";

const ACTIVE_DOC_STATUSES = new Set(["PENDING", "PARSING", "CHUNKING", "EMBEDDING", "DELETING"]);

export const knowledgeKeys = {
  all: ["knowledge"] as const,
  list: (filters: KnowledgeBaseFilters) => [...knowledgeKeys.all, "list", filters] as const,
  documents: (kbId: string) => [...knowledgeKeys.all, "documents", kbId] as const,
  chunks: (docId: string) => [...knowledgeKeys.all, "chunks", docId] as const,
};

export const taskKeys = {
  all: ["tasks"] as const,
  list: (filters: TaskFilters) => [...taskKeys.all, "list", filters] as const,
};

export function useKnowledgeBases(filters: KnowledgeBaseFilters = {}) {
  return useInfiniteQuery({
    queryKey: knowledgeKeys.list(filters),
    queryFn: ({ pageParam }) => listKnowledgeBases({ ...filters, cursor: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 15_000,
  });
}

export function useDocuments(kbId: string | undefined, filters: DocumentFilters = {}) {
  return useInfiniteQuery({
    queryKey: [...knowledgeKeys.documents(kbId ?? ""), filters],
    queryFn: ({ pageParam }) =>
      listDocuments(kbId as string, { ...filters, cursor: pageParam, limit: 20 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(kbId),
    staleTime: 0,
    refetchInterval: (query) => {
      const pages = query.state.data?.pages ?? [];
      const active = pages.some((page) =>
        page.items.some((document) => ACTIVE_DOC_STATUSES.has(document.status)),
      );
      return active ? 2_000 : false;
    },
  });
}

export function useChunks(docId: string | undefined, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: knowledgeKeys.chunks(docId ?? ""),
    queryFn: ({ pageParam }) =>
      listChunks(docId as string, { cursor: pageParam, limit: 30 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(docId) && enabled,
  });
}

export function useCreateKnowledgeBase() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: KnowledgeBaseInput) => createKnowledgeBase(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.all });
    },
  });
}

export function useUpdateKnowledgeBase() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Partial<KnowledgeBaseInput> }) =>
      updateKnowledgeBase(id, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.all });
    },
  });
}

export function useDeleteKnowledgeBase() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteKnowledgeBase(id, true),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.all });
      void queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}

export function useDeleteDocument(kbId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (docId: string) => deleteDocument(docId),
    onSuccess: () => {
      if (kbId) void queryClient.invalidateQueries({ queryKey: knowledgeKeys.documents(kbId) });
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.all });
      void queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}

export interface UploadVariables {
  file: File;
  onProgress?: (percent: number, loaded: number, total: number) => void;
  signal?: AbortSignal;
}

export function useUploadDocument(kbId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, onProgress, signal }: UploadVariables) =>
      uploadDocument(kbId as string, file, { onProgress, signal }),
    onSuccess: () => {
      if (kbId) void queryClient.invalidateQueries({ queryKey: knowledgeKeys.documents(kbId) });
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.all });
      void queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}

export function useSearchKnowledgeBase() {
  return useMutation({
    mutationFn: ({ kbId, request }: { kbId: string; request: SearchRequest }) =>
      searchKnowledgeBase(kbId, request),
  });
}
