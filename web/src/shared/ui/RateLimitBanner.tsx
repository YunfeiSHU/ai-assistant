import { useEffect, useState } from "react";

import { useRateLimitStore } from "@/shared/api/rate-limit";

export function useRateLimitRemaining(): number {
  const until = useRateLimitStore((state) => state.until);
  const clear = useRateLimitStore((state) => state.clear);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!until) return;
    const timer = window.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= until) clear();
    }, 1000);
    return () => window.clearInterval(timer);
  }, [until, clear]);

  return until ? Math.max(0, Math.ceil((until - now) / 1000)) : 0;
}

export function useRateLimitInfo(): { remaining: number | null; limit: number | null } {
  const remaining = useRateLimitStore((state) => state.remaining);
  const limit = useRateLimitStore((state) => state.limit);
  return { remaining, limit };
}

export function RateLimitBanner() {
  const remaining = useRateLimitRemaining();
  const message = useRateLimitStore((state) => state.message);
  if (remaining <= 0) return null;
  return (
    <div className="rate-limit-banner" role="status" data-testid="rate-limit-banner">
      <span className="dot" />
      <b>请求过快</b>
      <span>{message || "已被限流"}</span>
      <span className="mono">{remaining}s</span>
    </div>
  );
}
