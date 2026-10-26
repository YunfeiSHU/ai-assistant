import { apiRequest } from "@/shared/api/client";

import type { Quota, Usage } from "./types";

export function getQuota(): Promise<Quota> {
  return apiRequest<Quota>("/me/quota");
}

export function getUsage({
  from,
  to,
  metric,
  limit = 50,
}: {
  from: string;
  to: string;
  metric?: string;
  limit?: number;
}): Promise<Usage> {
  const params = new URLSearchParams();
  params.set("from", from);
  params.set("to", to);
  params.set("limit", String(limit));
  if (metric) params.set("metric", metric);
  return apiRequest(`/me/usage?${params.toString()}`);
}
