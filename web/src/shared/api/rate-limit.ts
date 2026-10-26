import { create } from "zustand";

interface RateLimitState {
  until: number | null;
  message: string;
  remaining: number | null;
  limit: number | null;
  resetAt: number | null;
  setRateLimit: (seconds: number, message: string) => void;
  clear: () => void;
}

export const useRateLimitStore = create<RateLimitState>((set) => ({
  until: null,
  message: "",
  remaining: null,
  limit: null,
  resetAt: null,
  setRateLimit: (seconds, message) => {
    const safe = Number.isFinite(seconds) && seconds > 0 ? seconds : 60;
    set({ until: Date.now() + safe * 1000, message, remaining: 0 });
  },
  clear: () => set({ until: null, message: "", remaining: null, limit: null, resetAt: null }),
}));

export function markRateLimited(seconds: number | undefined, message: string): void {
  useRateLimitStore.getState().setRateLimit(seconds ?? 60, message);
}

/**
 * 读取网关 B-07 的剩余额度头。
 * 只接受 message scope：登录/建会话的额度与聊天发送不是同一个桶，混用会误禁用。
 */
export function observeRateLimitHeaders(headers: Headers): void {
  if (headers.get("X-RateLimit-Scope") !== "message") return;
  const limit = Number(headers.get("X-RateLimit-Limit"));
  const remaining = Number(headers.get("X-RateLimit-Remaining"));
  const resetSeconds = Number(headers.get("X-RateLimit-Reset"));
  if (!Number.isFinite(limit) || !Number.isFinite(remaining)) return;
  const resetAt = Number.isFinite(resetSeconds) && resetSeconds > 0 ? resetSeconds * 1000 : null;
  useRateLimitStore.setState({
    limit,
    remaining: Math.max(0, remaining),
    resetAt,
  });
  if (remaining <= 0 && resetAt) {
    const seconds = Math.max(1, Math.ceil((resetAt - Date.now()) / 1000));
    markRateLimited(seconds, "本轮消息额度已用完");
  }
}

