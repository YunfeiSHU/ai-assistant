import { API_BASE } from "./base";
import type { TokenResponse } from "./types";

// B-08：刷新令牌只存在 HttpOnly Cookie 里，JS 不可读。
// 这个 key 只用于清理旧版本残留在 localStorage 的令牌。
const LEGACY_REFRESH_KEY = "ai.refresh.v1";

let accessToken: string | null = null;
let accessExpiresAt = 0;
let refreshInflight: Promise<boolean> | null = null;

function purgeLegacyRefresh(): void {
  try {
    window.localStorage.removeItem(LEGACY_REFRESH_KEY);
  } catch {
    // ignore
  }
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function setSession(tokens: TokenResponse): void {
  accessToken = tokens.access_token;
  accessExpiresAt = Date.now() + Math.max(0, tokens.expires_in - 60) * 1000;
  purgeLegacyRefresh();
}

export function clearSession(notify = false): void {
  accessToken = null;
  accessExpiresAt = 0;
  purgeLegacyRefresh();
  if (notify) {
    window.dispatchEvent(new Event("auth:expired"));
  }
}

/** 临近过期；F1 暂未使用，留给 F3 的主动续期。 */
export function isAccessExpiring(): boolean {
  return Boolean(accessToken) && Date.now() >= accessExpiresAt;
}

async function withLock<T>(fn: () => Promise<T>): Promise<T> {
  const locks = (
    navigator as Navigator & {
      locks?: { request: <R>(name: string, callback: () => Promise<R>) => Promise<R> };
    }
  ).locks;
  if (!locks) return fn();
  return locks.request("auth:refresh", fn);
}

async function doRefresh(): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } catch {
    return false;
  }

  if (!response.ok) {
    clearSession(true);
    return false;
  }

  const tokens = (await response.json()) as TokenResponse;
  setSession(tokens);
  return true;
}

/** 单飞刷新：并发 401 只发一次 /auth/refresh。 */
export function refreshOnce(): Promise<boolean> {
  refreshInflight ??= withLock(doRefresh).finally(() => {
    refreshInflight = null;
  });
  return refreshInflight;
}
