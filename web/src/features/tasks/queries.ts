import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { cancelTask, listTasks, retryTask } from "./api";
import type { Task, TaskFilters } from "./types";

export const taskKeys = {
  all: ["tasks"] as const,
  list: (filters: TaskFilters) => [...taskKeys.all, "list", filters] as const,
};

const ACTIVE_STATUSES = new Set(["PENDING", "QUEUED", "RUNNING"]);

export function isActiveTask(task: Task): boolean {
  return ACTIVE_STATUSES.has(task.status);
}

export function useTasks(filters: TaskFilters = {}) {
  return useInfiniteQuery({
    queryKey: taskKeys.list(filters),
    queryFn: ({ pageParam }) => listTasks({ ...filters, cursor: pageParam, limit: 30 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 0,
    refetchInterval: (query) => {
      const pages = query.state.data?.pages ?? [];
      const active = pages.some((page) => page.items.some(isActiveTask));
      return active ? 2_000 : false;
    },
  });
}

export function useCancelTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => cancelTask(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}

export function useRetryTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => retryTask(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}
