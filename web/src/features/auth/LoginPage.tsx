import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useNavigate } from "@tanstack/react-router";

import { isApiError } from "@/shared/api/error";
import { useAuthStore } from "./store";

type Mode = "login" | "register";

export function LoginPage() {
  const navigate = useNavigate();
  const status = useAuthStore((state) => state.status);
  const login = useAuthStore((state) => state.login);
  const register = useAuthStore((state) => state.register);

  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [nickname, setNickname] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (status === "authed") {
      void navigate({ to: "/chat", replace: true });
    }
  }, [status, navigate]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (mode === "login") {
        await login({ email, password });
      } else {
        await register({ email, password, nickname: nickname || undefined });
      }
      await navigate({ to: "/chat", replace: true });
    } catch (cause) {
      setError(
        isApiError(cause)
          ? `${cause.code} · ${cause.message}`
          : "请求失败，请检查网关是否启动",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="auth">
      <section className="auth-left">
        <div className="brand brand-lg">
          <div className="logo">AI</div>
          <div className="brand-text">
            <strong>AI Assistant</strong>
            <span>个人智能工作台</span>
          </div>
        </div>

        <div className="auth-copy">
          <p className="eyebrow">AI 工作台 · 对话与知识</p>
          <h1>
            把对话、资料与记忆，
            <br />
            收进一个工作台。
          </h1>
          <p className="lead">
            AI Assistant 把流式对话、知识库引用、长期记忆与用量配额放在同一个界面里。
            登录后即可开始。
          </p>
          <ul className="feature-list">
            <li>
              <span className="tick">✓</span> 流式回答，随时停止或重新生成
            </li>
            <li>
              <span className="tick">✓</span> 知识库检索与引用，可回看原文
            </li>
            <li>
              <span className="tick">✓</span> 长期记忆与会话上下文分开管理
            </li>
            <li>
              <span className="tick">✓</span> 配额与用量透明，异常有明确提示
            </li>
          </ul>
        </div>

        <p className="auth-note">个人智能工作台 · 数据保存在你的账号下</p>
      </section>

      <section className="auth-right">
        <form className="auth-card" onSubmit={handleSubmit} autoComplete="off">
          <div className="auth-tabs">
            <button
              type="button"
              className={mode === "login" ? "active" : ""}
              onClick={() => {
                setMode("login");
                setError(null);
              }}
            >
              登录
            </button>
            <button
              type="button"
              className={mode === "register" ? "active" : ""}
              onClick={() => {
                setMode("register");
                setError(null);
              }}
            >
              注册
            </button>
          </div>

          <h2>{mode === "login" ? "欢迎回来" : "创建账号"}</h2>
          <p className="auth-sub">
            {mode === "login"
              ? "使用你的账号登录；首次使用可先注册。"
              : "注册成功会直接进入工作台；密码至少 8 位。"}
          </p>

          {mode === "register" && (
            <label className="field">
              <span>昵称</span>
              <input
                name="nickname"
                data-testid="auth-nickname"
                value={nickname}
                onChange={(event) => setNickname(event.target.value)}
                placeholder="怎么称呼你"
                autoComplete="nickname"
              />
            </label>
          )}

          <label className="field">
            <span>邮箱</span>
            <input
              name="email"
              type="email"
              data-testid="auth-email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
              required
            />
          </label>

          <label className="field">
            <span>密码</span>
            <input
              name="password"
              type="password"
              data-testid="auth-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="至少 8 位"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              minLength={8}
              required
            />
          </label>

          <div className="form-error" aria-live="polite" data-testid="auth-error">
            {error}
          </div>

          <button
            className="btn primary block"
            type="submit"
            data-testid="auth-submit"
            disabled={loading}
          >
            {loading ? "处理中…" : mode === "login" ? "登录并进入" : "注册并进入"}
          </button>

          <p className="auth-foot">登录后进入个人工作台</p>
        </form>
      </section>
    </div>
  );
}
