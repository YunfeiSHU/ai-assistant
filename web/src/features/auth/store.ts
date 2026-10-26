import { create } from "zustand";

import { apiRequest } from "@/shared/api/client";
import {
  clearSession,
  getAccessToken,
  refreshOnce,
  setSession,
} from "@/shared/api/session";
import type {
  LoginInput,
  RegisterInput,
  TokenResponse,
  User,
} from "@/shared/api/types";

export type AuthStatus = "booting" | "anonymous" | "authed";

interface AuthState {
  status: AuthStatus;
  user: User | null;
  bootstrap: () => Promise<void>;
  login: (input: LoginInput) => Promise<void>;
  register: (input: RegisterInput) => Promise<void>;
  logout: () => Promise<void>;
}

let bootstrapPromise: Promise<void> | null = null;

async function loadUser(): Promise<User> {
  return apiRequest<User>("/me");
}

export const useAuthStore = create<AuthState>((set, get) => ({
  status: "booting",
  user: null,

  bootstrap: () => {
    bootstrapPromise ??= (async () => {
      // B-08：浏览器是否持有 refresh 只能由服务端 Cookie 判断，JS 看不到。
      // 所以启动时无条件尝试一次；没有/过期会 401，走 anonymous。
      const refreshed = await refreshOnce();
      if (!refreshed) {
        set({ status: "anonymous", user: null });
        return;
      }

      try {
        const user = await loadUser();
        set({ status: "authed", user });
      } catch {
        clearSession();
        set({ status: "anonymous", user: null });
      }
    })();

    return bootstrapPromise;
  },

  login: async (input) => {
    const tokens = await apiRequest<TokenResponse>("/auth/login", {
      method: "POST",
      auth: false,
      json: { ...input, device_name: "web" },
    });
    setSession(tokens);
    const user = tokens.user ?? (await loadUser());
    set({ status: "authed", user });
  },

  register: async (input) => {
    const tokens = await apiRequest<TokenResponse>("/auth/register", {
      method: "POST",
      auth: false,
      json: input,
    });

    if (!tokens.access_token) {
      await get().login({ email: input.email, password: input.password });
      return;
    }

    setSession(tokens);
    const user = tokens.user ?? (await loadUser());
    set({ status: "authed", user });
  },

  logout: async () => {
    try {
      // access 可能已过期；先用 Cookie refresh 一次，确保 logout 能通过鉴权。
      if (!getAccessToken()) {
        await refreshOnce();
      }
      await apiRequest<void>("/auth/logout", { method: "POST" });
    } catch {
      // 登出失败也要清本地：服务端 refresh 可能已失效。
    } finally {
      clearSession();
      set({ status: "anonymous", user: null });
    }
  },
}));

if (typeof window !== "undefined") {
  window.addEventListener("auth:expired", () => {
    useAuthStore.setState({ status: "anonymous", user: null });
  });
}
