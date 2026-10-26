import { useEffect, useMemo, useState } from "react";

import { Outlet, useNavigate, useRouterState } from "@tanstack/react-router";

import { useAuthStore } from "@/features/auth/store";
import {
  useConversationList,
  useCreateConversation,
  useMessageSearch,
} from "@/features/conversations/queries";
import type { User } from "@/shared/api/types";
import { errorText } from "@/shared/api/error";
import { formatRelative, formatSidebarTime, initialOf } from "@/shared/lib/format";
import { Icon } from "@/shared/ui/Icon";
import { RateLimitBanner } from "@/shared/ui/RateLimitBanner";
import { Toaster, toast } from "@/shared/ui/toast";

const NAV = [
  { to: "/chat", label: "对话", icon: "spark" },
  { to: "/knowledge", label: "知识库", icon: "book" },
  { to: "/tasks", label: "任务", icon: "task" },
  { to: "/memory", label: "记忆", icon: "brain" },
  { to: "/quota", label: "配额", icon: "gauge" },
] as const;

const TITLES: Record<string, string> = {
  "/chat": "工作台",
  "/knowledge": "知识库",
  "/tasks": "任务",
  "/memory": "长期记忆",
  "/quota": "配额与用量",
  "/settings": "设置",
};

/** 认证门：不挂任何数据查询，避免在 access token 就绪前打出 401。 */
export function AppLayout() {
  const status = useAuthStore((state) => state.status);
  const user = useAuthStore((state) => state.user);
  const navigate = useNavigate();

  useEffect(() => {
    if (status === "anonymous") {
      void navigate({ to: "/login", replace: true });
    }
  }, [status, navigate]);

  if (status === "booting") {
    return (
      <div className="boot-splash">
        <div className="logo">AI</div>
        <p>正在恢复会话…</p>
      </div>
    );
  }

  if (status !== "authed" || !user) return null;

  return <AppShell user={user} />;
}

function AppShell({ user }: { user: User }) {
  const logout = useAuthStore((state) => state.logout);
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const [keyword, setKeyword] = useState("");
  const [debouncedKeyword, setDebouncedKeyword] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedKeyword(keyword), 280);
    return () => window.clearTimeout(timer);
  }, [keyword]);

  const conversationsQuery = useConversationList({
    keyword: debouncedKeyword || undefined,
    limit: 20,
  });
  const createConversation = useCreateConversation();
  const messageSearchQuery = useMessageSearch(debouncedKeyword);
  const messageHits = messageSearchQuery.data?.items ?? [];

  const conversations = useMemo(() => {
    const seen = new Set<string>();
    const items = [];
    for (const page of conversationsQuery.data?.pages ?? []) {
      for (const conversation of page.items) {
        if (seen.has(conversation.id)) continue;
        seen.add(conversation.id);
        items.push(conversation);
      }
    }
    return items;
  }, [conversationsQuery.data]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        document.getElementById("conv-search")?.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const title = TITLES[pathname] ?? "工作台";
  const initial = initialOf(user.nickname || user.email);
  const activeId = pathname.startsWith("/chat/") ? pathname.slice("/chat/".length) : null;

  async function handleLogout() {
    await logout();
    await navigate({ to: "/login", replace: true });
  }

  function startNewChat() {
    setSidebarOpen(false);
    createConversation.mutate(
      {},
      {
        onSuccess: (conversation) => {
          void navigate({
            to: "/chat/$conversationId",
            params: { conversationId: conversation.id },
          });
        },
        onError: (error) => toast(errorText(error), "bad"),
      },
    );
  }

  return (
    <div className={`app rail-off${sidebarOpen ? " sidebar-open" : ""}`}>
      <div
        className="overlay"
        role="presentation"
        onClick={() => setSidebarOpen(false)}
      />

      <aside className="sidebar">
        <div className="brand">
          <div className="logo">AI</div>
          <div className="brand-text">
            <strong>AI Assistant</strong>
            <span>个人智能工作台</span>
          </div>
          <button
            className="icon-btn only-mobile"
            type="button"
            aria-label="关闭侧栏"
            onClick={() => setSidebarOpen(false)}
            style={{ marginLeft: "auto" }}
          >
            <Icon name="x" />
          </button>
        </div>

        <button
          className="btn primary block new-chat"
          type="button"
          onClick={startNewChat}
          disabled={createConversation.isPending}
        >
          <Icon name="plus" />
          <span className="label">
            {createConversation.isPending ? "创建中…" : "新建对话"}
          </span>
        </button>

        <div className="search">
          <span className="search-icon">
            <Icon name="search" />
          </span>
          <input
            id="conv-search"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder="搜索会话"
            aria-label="搜索会话"
          />
          <kbd>⌘K</kbd>
        </div>

        <div className="conv-section">
          <div className="section-label">
            最近对话
            {conversations.length ? <span> · {conversations.length}</span> : null}
          </div>
          <div className="conv-list">
            {conversationsQuery.isPending ? (
              <div className="conv-empty">正在加载会话…</div>
            ) : null}

            {conversationsQuery.isError ? (
              <div className="conv-empty">
                会话加载失败
                <div style={{ marginTop: 8 }}>
                  <button
                    className="btn ghost sm"
                    type="button"
                    onClick={() => void conversationsQuery.refetch()}
                  >
                    重试
                  </button>
                </div>
              </div>
            ) : null}

            {!conversationsQuery.isPending &&
            !conversationsQuery.isError &&
            conversations.length === 0 ? (
              <div className="conv-empty">
                {keyword ? "没有匹配的会话" : "还没有会话，点上方新建"}
              </div>
            ) : null}

            {conversations.map((conversation) => {
              const active = activeId === conversation.id;
              return (
                <div
                  key={conversation.id}
                  className={`conv-item${active ? " active" : ""}${
                    conversation.status === "archived" ? " archived" : ""
                  }`}
                  role="button"
                  tabIndex={0}
                  data-testid="conv-item"
                  onClick={() => {
                    setSidebarOpen(false);
                    void navigate({
                      to: "/chat/$conversationId",
                      params: { conversationId: conversation.id },
                    });
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      void navigate({
                        to: "/chat/$conversationId",
                        params: { conversationId: conversation.id },
                      });
                    }
                  }}
                >
                  <span className="conv-dot" />
                  <div className="conv-main">
                    <div className="title">
                      {conversation.pinned ? <Icon name="pin" size={11} /> : null}
                      {conversation.title || "未命名对话"}
                    </div>
                    <div className="preview">
                      {conversation.last_message_preview || `${conversation.message_count} 条消息`}
                    </div>
                    <div className="conv-meta">
                      {conversation.last_message_preview
                        ? `${conversation.message_count} 条消息`
                        : ""}
                      {conversation.status === "archived" ? " · 已归档" : ""}
                      {conversation.last_message_at
                        ? ` · ${formatRelative(conversation.last_message_at)}`
                        : ""}
                    </div>
                  </div>
                  <span className="time">{formatSidebarTime(conversation.last_message_at)}</span>
                </div>
              );
            })}

            {debouncedKeyword && messageHits.length ? (
              <>
                <div className="section-label" style={{ marginTop: 12 }}>
                  消息命中 · {messageHits.length}
                </div>
                <div className="message-hits">
                  {messageHits.map((hit) => (
                    <div
                      className="message-hit"
                      key={hit.message_id}
                      role="button"
                      tabIndex={0}
                      data-testid="message-hit"
                      onClick={() => {
                        setSidebarOpen(false);
                        void navigate({
                          to: "/chat/$conversationId",
                          params: { conversationId: hit.conversation_id },
                        });
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSidebarOpen(false);
                          void navigate({
                            to: "/chat/$conversationId",
                            params: { conversationId: hit.conversation_id },
                          });
                        }
                      }}
                    >
                      <b>{hit.title || "未命名对话"}</b>
                      <span>{hit.snippet}</span>
                    </div>
                  ))}
                </div>
              </>
            ) : null}

            {conversationsQuery.hasNextPage ? (
              <button
                className="btn ghost sm block"
                type="button"
                data-testid="conv-more"
                onClick={() => void conversationsQuery.fetchNextPage()}
                disabled={conversationsQuery.isFetchingNextPage}
              >
                {conversationsQuery.isFetchingNextPage ? "加载中…" : "加载更多"}
              </button>
            ) : null}
          </div>
        </div>

        <nav className="side-nav" aria-label="主导航">
          {NAV.map((item) => {
            const active = pathname === item.to || pathname.startsWith(`${item.to}/`);
            return (
              <button
                key={item.to}
                type="button"
                className={active ? "active" : ""}
                onClick={() => {
                  setSidebarOpen(false);
                  void navigate({ to: item.to });
                }}
              >
                <Icon name={item.icon} />
                <span className="label">{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="user-card">
          <div className="avatar">{initial}</div>
          <div className="user-info">
            <strong data-testid="user-nickname">{user.nickname || user.email}</strong>
            <span>
              {user.plan} · {user.email}
            </span>
          </div>
          <button
            className="icon-btn"
            type="button"
            title="退出登录"
            aria-label="退出登录"
            data-testid="logout"
            onClick={() => {
              void handleLogout();
            }}
          >
            <Icon name="logout" />
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <button
            className="icon-btn only-mobile"
            type="button"
            aria-label="打开侧栏"
            onClick={() => setSidebarOpen(true)}
          >
            <Icon name="menu" />
          </button>
          <div className="page-heading">
            <h1>{title}</h1>
            <p>已连接 · {user.plan}</p>
          </div>
          <div className="topbar-actions">
            <span className="chip ok">已认证</span>
            <button
              className="icon-btn"
              type="button"
              aria-label="设置"
              onClick={() => {
                void navigate({ to: "/settings" });
              }}
            >
              <Icon name="settings" />
            </button>
          </div>
        </header>
        <main id="viewRoot">
          <Outlet />
        </main>
      </div>

      <RateLimitBanner />
      <Toaster />
    </div>
  );
}
