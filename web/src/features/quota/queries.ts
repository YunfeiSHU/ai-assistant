import { useQuery } from "@tanstack/react-query";

import { getQuota, getUsage } from "./api";

export const quotaKeys = {
  quota: ["quota"] as const,
  usage: (from: string, to: string) => ["usage", from, to] as const,
};

export function useQuota() {
  return useQuery({
    queryKey: quotaKeys.quota,
    queryFn: getQuota,
    staleTime: 10_000,
    refetchInterval: 30_000,
  });
}

export function useUsage(from: string, to: string) {
  return useQuery({
    queryKey: quotaKeys.usage(from, to),
    queryFn: () => getUsage({ from, to, limit: 50 }),
    staleTime: 10_000,
  });
}
