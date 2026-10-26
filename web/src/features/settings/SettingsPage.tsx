import { useEffect, useState } from "react";

import { useNavigate } from "@tanstack/react-router";

import { useAuthStore } from "@/features/auth/store";
import { apiRequest } from "@/shared/api/client";
import { isApiError } from "@/shared/api/error";
import { clearSession } from "@/shared/api/session";
import { Icon } from "@/shared/ui/Icon";
import { toast } from "@/shared/ui/toast";

type Theme = "dark" | "light";

function errorText(error: unknown): string {
  if (isApiError(error)) return `${error.code} · ${error.message}`;
  return error instanceof Error ? error.message : "请求失败，请稍后重试";
}

export function SettingsPage() {
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const navigate = useNavigate();

  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [changing, setChanging] = useState(false);

  const [theme, setTheme] = useState<Theme>(() => {
    const saved = window.localStorage.getItem("ai.theme.v1");
    return saved === "light" ? "light" : "dark";
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("ai.theme.v1", theme);
  }, [theme]);

  async function changePassword() {
    setPasswordError("");
    if (!oldPassword || !newPassword) {
      setPasswordError("请填写旧密码与新密码");
      return;
    }
    if (newPassword.length < 8) {
      setPasswordError("新密码至少 8 位");
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("两次输入的新密码不一致");
      return;
    }
    if (newPassword === oldPassword) {
      setPasswordError("新密码不能与旧密码相同");
      return;
    }

    setChanging(true);
    try {
      await apiRequest<void>("/auth/password", {
        method: "POST",
        json: { old_password: oldPassword, new_password: newPassword },
      });
      clearSession();
      useAuthStore.setState({ status: "anonymous", user: null });
      toast("密码已修改，请重新登录", "ok");
      await navigate({ to: "/login", replace: true });
    } catch (error) {
      setPasswordError(errorText(error));
    } finally {
      setChanging(false);
    }
  }

  return (
    <div className="page">
      <div className="page-inner">
        <div className="panel">
          <div className="panel-head">
            <div>
              <h2>账号</h2>
              <p>来自网关 GET /me 的真实数据。</p>
            </div>
            <span className="chip ok">已认证</span>
          </div>
          <div className="kv">
            <span>用户 ID</span>
            <b>{user?.id}</b>
          </div>
          <div className="kv">
            <span>邮箱</span>
            <b>{user?.email}</b>
          </div>
          <div className="kv">
            <span>昵称</span>
            <b>{user?.nickname || "—"}</b>
          </div>
          <div className="kv">
            <span>套餐</span>
            <b>{user?.plan}</b>
          </div>
          <div className="kv">
            <span>创建时间</span>
            <b>{user?.created_at}</b>
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
            <button
              className="btn danger sm"
              type="button"
              data-testid="logout-settings"
              onClick={() => {
                void logout();
              }}
            >
              <Icon name="logout" size={14} />
              退出登录
            </button>
          </div>
        </div>

        <div className="grid-2">
          <div className="panel">
            <div className="panel-head">
              <div>
                <h2>安全 · 修改密码</h2>
                <p>改密后旧 access 与全部 refresh 失效，需要重新登录。</p>
              </div>
            </div>
            <label className="field">
              <span>旧密码</span>
              <input
                type="password"
                data-testid="old-password"
                value={oldPassword}
                onChange={(event) => setOldPassword(event.target.value)}
                autoComplete="current-password"
              />
            </label>
            <label className="field">
              <span>新密码</span>
              <input
                type="password"
                data-testid="new-password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                autoComplete="new-password"
              />
            </label>
            <label className="field">
              <span>确认新密码</span>
              <input
                type="password"
                data-testid="confirm-password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                autoComplete="new-password"
              />
            </label>
            {passwordError ? (
              <div className="form-error" data-testid="password-error">
                {passwordError}
              </div>
            ) : null}
            <button
              className="btn primary sm"
              type="button"
              data-testid="change-password"
              onClick={() => void changePassword()}
              disabled={changing}
            >
              {changing ? "提交中…" : "修改密码"}
            </button>
          </div>

          <div className="panel">
            <div className="panel-head">
              <div>
                <h2>外观</h2>
                <p>主题写入 localStorage，刷新后保持。</p>
              </div>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                className={`btn ghost sm${theme === "dark" ? " active" : ""}`}
                type="button"
                data-testid="theme-dark"
                onClick={() => setTheme("dark")}
              >
                暗色
              </button>
              <button
                className={`btn ghost sm${theme === "light" ? " active" : ""}`}
                type="button"
                data-testid="theme-light"
                onClick={() => setTheme("light")}
              >
                亮色
              </button>
            </div>
            <div className="search-meta" style={{ marginTop: 10 }}>
              当前：{theme === "dark" ? "暗色" : "亮色"}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
