export interface QuotaMetric {
  metric: string;
  limit: number | null;
  used: number;
  remaining: number;
  unit: string;
  reset_at: string | null;
}

export interface Quota {
  plan: string;
  period: { start: string; end: string };
  metrics: QuotaMetric[];
}

export interface UsageItem {
  id: number;
  metric: string;
  amount: number;
  conversation_id: string | null;
  message_id: string | null;
  trace_id: string | null;
  created_at: string;
}

export interface Usage {
  from: string;
  to: string;
  metric: string | null;
  items: UsageItem[];
  totals: {
    chat_requests: number;
    llm_tokens: number;
  };
}
