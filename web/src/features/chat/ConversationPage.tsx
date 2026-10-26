import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useNavigate, useParams } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";

import {
  conversationKeys,
  useArchiveConversation,
  useConversation,
  useDeleteConversation,
  useUpdateConversation,
} from "@/features/conversations/queries";
import {
  useMessages,
  useDeleteMessage,
  useRegenerateMessage,
  useSyncAfterSeq,
} from "@/features/messages/queries";
import type { Message } from "@/features/messages/types";
import { useDocuments } from "@/features/knowledge/queries";
import { isApiError } from "@/shared/api/error";
import { formatDuration, formatRelative, formatTime } from "@/shared/lib/format";
import { Icon } from "@/shared/ui/Icon";
import { Markdown } from "@/shared/ui/Markdown";
import { Modal } from "@/shared/ui/Modal";
import { useRateLimitInfo, useRateLimitRemaining } from "@/shared/ui/RateLimitBanner";
import { toast } from "@/shared/ui/toast";

import { LiveTurn } from "./LiveTurn";
import { ReferenceDetails } from "./ReferenceDetails";
import { StreamErrorCard } from "./StreamErrorCard";
import { ToolTrace } from "./ToolTrace";
import { ContextModal, KbSelectModal } from "./ConversationTools";
import { useChatStream } from "./stream/useChatStream";

function errorMessage(error: unknown): string {
  if (isApiError(error)) return `${error.code} · ${error.message}`;
  return "请求失败，请稍后重试";
}

function MessageItem({
  message,
  onCitation,
  onDelete,
  onRegenerate,
}: {
  message: Message;
  onCitation: (messageId: string, index: number) => void;
  onDelete: (message: Message) => void;
  onRegenerate: (message: Message) => void;
}) {
  if (message.role === "user") {
    return (
      <article className="msg user" data-testid="message-item">
        <div>
          <div className="bubble">{message.content}</div>
          <div className="msg-meta" style={{ justifyContent: "flex-end" }}>
            <span>{formatTime(message.created_at)}</span>
            <button
              className="icon-btn msg-delete"
              type="button"
              title="删除这条消息"
              data-testid="message-delete"
              onClick={() => onDelete(message)}
            >
              <Icon name="trash" />
            </button>
          </div>
        </div>
      </article>
    );
  }

  const tokens = message.usage?.total_tokens ?? 0;
  return (
    <article className="msg assistant" data-testid="message-item">
      <div className="msg-avatar">AI</div>
      <div className="msg-body">
        <div className="msg-top">
          <span className="model-badge">{message.model || "—"}</span>
          <span className="msg-time">{formatTime(message.created_at)}</span>
          {message.status === "partial" ? (
            <span className="chip warn" data-testid="partial-chip">
              partial
            </span>
          ) : null}
          {message.status === "failed" ? <span className="chip bad">failed</span> : null}
          {message.degraded ? <span className="chip warn">degraded</span> : null}
        </div>

        <ToolTrace tools={message.tool_calls ?? []} />

        <Markdown
          content={message.content || "（无内容）"}
          onCitation={(index) => onCitation(message.id, index)}
        />

        <ReferenceDetails messageId={message.id} references={message.references ?? []} />

        <div className="msg-meta">
          <span>{tokens} tokens</span>
          <span className="sep">·</span>
          <span>{formatDuration(message.elapsed_ms)}</span>
          {message.finish_reason ? (
            <>
              <span className="sep">·</span>
              <span>{message.finish_reason}</span>
            </>
          ) : null}
          {message.tool_calls?.length ? (
            <>
              <span className="sep">·</span>
              <span>{message.tool_calls.length} 次工具调用</span>
            </>
          ) : null}
          <button
            className="icon-btn msg-delete"
            type="button"
            title="重新生成"
            data-testid="message-regenerate"
            onClick={() => onRegenerate(message)}
          >
            <Icon name="refresh" />
          </button>
          <button
            className="icon-btn msg-delete"
            type="button"
            title="删除这条消息"
            data-testid="message-delete"
            onClick={() => onDelete(message)}
          >
            <Icon name="trash" />
          </button>
        </div>
      </div>
    </article>
  );
}

export function ConversationPage() {
  const navigate = useNavigate();
  const params = useParams({ strict: false }) as { conversationId?: string };
  const conversationId = params.conversationId;

  const conversationQuery = useConversation(conversationId);
  const messagesQuery = useMessages(conversationId);
  const deleteMessageMutation = useDeleteMessage(conversationId);
  const regenerateMessageMutation = useRegenerateMessage(conversationId);
  const syncAfterSeq = useSyncAfterSeq(conversationId);
  const queryClient = useQueryClient();
  const updateConversation = useUpdateConversation();
  const archiveConversation = useArchiveConversation();
  const deleteConversation = useDeleteConversation();

  const handleSettled = useCallback(async () => {
    await Promise.all([messagesQuery.refetch(), conversationQuery.refetch()]);
    // 侧栏列表也要更新 message_count / last_message_at，否则停在旧值。
    void queryClient.invalidateQueries({ queryKey: [...conversationKeys.all, "list"] });
  }, [messagesQuery.refetch, conversationQuery.refetch, queryClient]);

  const stream = useChatStream(conversationId, handleSettled);
  const { turn, send, stop, reset, isStreaming } = stream;
  const rateLimitRemaining = useRateLimitRemaining();
  const rateLimitInfo = useRateLimitInfo();

  const [draft, setDraft] = useState("");
  const [useRag, setUseRag] = useState(true);
  const [useMemory, setUseMemory] = useState(true);
  const [useTools, setUseTools] = useState(false);

  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [messageToDelete, setMessageToDelete] = useState<Message | null>(null);
  const [kbOpen, setKbOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [attachmentOpen, setAttachmentOpen] = useState(false);
  const [attachmentDocIDs, setAttachmentDocIDs] = useState<string[]>([]);
  const [titleDraft, setTitleDraft] = useState("");

  const scrollRef = useRef<HTMLDivElement>(null);
  const initialScrolled = useRef<string | null>(null);

  const conversation = conversationQuery.data;
  const attachmentKbId = conversation?.kb_ids?.[0];
  const attachmentDocsQuery = useDocuments(
    attachmentOpen ? attachmentKbId : undefined,
    { limit: 50 },
  );
  const attachmentDocs = useMemo(
    () =>
      (attachmentDocsQuery.data?.pages ?? [])
        .flatMap((page) => page.items)
        .filter((doc) => doc.status === "INDEXED"),
    [attachmentDocsQuery.data],
  );

  const messages = useMemo(() => {
    const pages = messagesQuery.data?.pages ?? [];
    const seen = new Set<string>();
    const flat: Message[] = [];
    for (const page of pages) {
      for (const message of page.items) {
        if (seen.has(message.id)) continue;
        seen.add(message.id);
        flat.push(message);
      }
    }
    return flat.reverse();
  }, [messagesQuery.data]);

  const assistantPersisted = Boolean(
    turn.assistantId && messages.some((message) => message.id === turn.assistantId),
  );

  const liveVisible =
    turn.phase !== "idle" &&
    !assistantPersisted &&
    !(turn.phase === "error" && (turn.content.length === 0 || !messagesQuery.isFetching));

  useEffect(() => {
    reset();
    setDraft("");
  }, [conversationId, reset]);

  useEffect(
    () => () => {
      stop();
    },
    [conversationId, stop],
  );

  useEffect(() => {
    if (assistantPersisted && (turn.phase === "done" || turn.phase === "aborted")) {
      reset();
    }
  }, [assistantPersisted, turn.phase, reset]);

  // 停止时服务端落 partial 可能晚于第一次 refetch：短暂轮询，直到 assistant 出现在台账里。
  useEffect(() => {
    if (!turn.assistantId || assistantPersisted) return;
    if (turn.phase !== "aborted" && turn.phase !== "done") return;

    let cancelled = false;
    let attempts = 0;
    let timer: number | null = null;

    const poll = async () => {
      if (cancelled) return;
      attempts += 1;
      await messagesQuery.refetch();
      if (cancelled) return;
      if (attempts < 12) {
        timer = window.setTimeout(() => void poll(), 250);
      }
    };

    timer = window.setTimeout(() => void poll(), 250);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [turn.assistantId, turn.phase, assistantPersisted, messagesQuery.refetch]);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const nearBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight < 220;
    if (nearBottom) element.scrollTop = element.scrollHeight;
  }, [turn.content.length, turn.phase, messages.length]);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element || !conversationId) return;
    if (messagesQuery.isPending) return;
    if (initialScrolled.current === conversationId) return;
    initialScrolled.current = conversationId;
    element.scrollTop = element.scrollHeight;
  }, [conversationId, messagesQuery.isPending, messages.length]);

  function handleRegenerate(message: Message) {
    if (regenerateMessageMutation.isPending) return;
    regenerateMessageMutation.mutate(message.id, {
      onSuccess: () => toast("已重新生成回答", "ok"),
      onError: (error) => toast(errorMessage(error), "bad"),
    });
  }

  async function handleSyncMessages() {
    try {
      const count = await syncAfterSeq();
      toast(count > 0 ? `已加载 ${count} 条新消息` : "已经是最新消息", count > 0 ? "ok" : "warn");
    } catch (error) {
      toast(errorMessage(error), "bad");
    }
  }

  function handleCitation(messageId: string, index: number) {    const details = document.getElementById(`refs-${messageId}`) as HTMLDetailsElement | null;
    if (details) details.open = true;
    const target = document.getElementById(`ref-${messageId}-${index}`);
    if (!target) return;
    target.classList.add("cite-flash");
    // 等 details 展开后再滚动，否则目标高度还是 0，资料卡会停在输入框后面。
    requestAnimationFrame(() => {
      target.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    window.setTimeout(() => target.classList.remove("cite-flash"), 1200);
  }

  function handleSend() {
    if (!conversation || conversation.status === "archived") return;
    if (rateLimitRemaining > 0) {
      toast(`请求过快，约 ${rateLimitRemaining}s 后恢复`, "warn");
      return;
    }
    if (isStreaming) {
      toast("正在生成中，先停止再发送", "warn");
      return;
    }
    const content = draft.trim();
    if (!content) {
      toast("先写点什么", "warn");
      return;
    }

    setDraft("");
    void send({
      content,
      use_rag: useRag || attachmentDocIDs.length > 0,
      use_memory: useMemory,
      use_tools: useTools,
      model: conversation.model ?? undefined,
      attachment_doc_ids: attachmentDocIDs,
    });
    setAttachmentDocIDs([]);
    requestAnimationFrame(() => {
      const element = scrollRef.current;
      if (element) element.scrollTop = element.scrollHeight;
    });
  }

  function handlePin() {
    if (!conversation) return;
    updateConversation.mutate(
      { id: conversation.id, patch: { pinned: !conversation.pinned } },
      {
        onSuccess: () => toast(conversation.pinned ? "已取消置顶" : "已置顶", "ok"),
        onError: (error) => toast(errorMessage(error), "bad"),
      },
    );
  }

  function openRename() {
    if (!conversation) return;
    setTitleDraft(conversation.title);
    setRenameOpen(true);
  }

  function saveRename() {
    if (!conversation) return;
    const title = titleDraft.trim();
    if (!title) {
      toast("标题不能为空", "warn");
      return;
    }
    updateConversation.mutate(
      { id: conversation.id, patch: { title } },
      {
        onSuccess: () => {
          setRenameOpen(false);
          toast("已重命名", "ok");
        },
        onError: (error) => toast(errorMessage(error), "bad"),
      },
    );
  }

  function toggleArchive() {
    if (!conversation) return;
    const archived = conversation.status !== "archived";
    archiveConversation.mutate(
      { id: conversation.id, archived },
      {
        onSuccess: () => toast(archived ? "已归档" : "已取消归档", "ok"),
        onError: (error) => toast(errorMessage(error), "bad"),
      },
    );
  }

  function confirmDelete() {
    if (!conversation) return;
    deleteConversation.mutate(conversation.id, {
      onSuccess: () => {
        setDeleteOpen(false);
        toast("会话已删除", "ok");
        void navigate({ to: "/chat" });
      },
      onError: (error) => toast(errorMessage(error), "bad"),
    });
  }

  async function loadOlder() {
    const element = scrollRef.current;
    const previousHeight = element?.scrollHeight ?? 0;
    await messagesQuery.fetchNextPage();
    requestAnimationFrame(() => {
      if (!element) return;
      element.scrollTop = element.scrollHeight - previousHeight;
    });
  }

  if (!conversationId) return null;

  if (conversationQuery.isPending) {
    return (
      <div className="page">
        <div className="page-inner">
          <div className="panel">正在加载会话…</div>
        </div>
      </div>
    );
  }

  if (conversationQuery.isError || !conversation) {
    const error = conversationQuery.error;
    return (
      <div className="page">
        <div className="page-inner">
          <div className="panel">
            <div className="panel-head">
              <div>
                <h2>无法打开会话</h2>
                <p>{errorMessage(error)}</p>
              </div>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                className="btn ghost sm"
                type="button"
                onClick={() => void conversationQuery.refetch()}
              >
                <Icon name="refresh" />
                重试
              </button>
              <button
                className="btn ghost sm"
                type="button"
                onClick={() => void navigate({ to: "/chat" })}
              >
                返回工作台
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const isArchived = conversation.status === "archived";

  return (
    <section className="chat-view">
      <div className="chat-scroll" ref={scrollRef}>
        <div className="chat-inner">
          {isArchived ? (
            <div className="archived-banner" data-testid="archived-banner">
              <div>
                <strong>该会话已归档</strong>
                <span>归档会话不能继续发送消息；取消归档后恢复。</span>
              </div>
              <button
                className="btn ghost sm"
                type="button"
                data-testid="unarchive"
                onClick={toggleArchive}
                disabled={archiveConversation.isPending}
              >
                <Icon name="archive" />
                取消归档
              </button>
            </div>
          ) : null}

          <div className="chat-page-head">
            <div>
              <h2 data-testid="conversation-title">{conversation.title || "未命名对话"}</h2>
              <p>
                {conversation.message_count} 条消息 · {isArchived ? "已归档" : "进行中"} ·{" "}
                {formatRelative(conversation.last_message_at)}
                {isStreaming ? " · 正在生成" : ""}
              </p>
            </div>
            <div className="chat-actions">
              <button
                className={`btn ghost sm${conversation.pinned ? " active" : ""}`}
                type="button"
                data-testid="pin"
                onClick={handlePin}
                disabled={updateConversation.isPending}
              >
                <Icon name="pin" />
                {conversation.pinned ? "已置顶" : "置顶"}
              </button>
              <button
                className="btn ghost sm"
                type="button"
                data-testid="sync-messages"
                title="只拉取 seq 之后的新消息"
                onClick={() => void handleSyncMessages()}
              >
                <Icon name="refresh" />
                增量刷新
              </button>
              <button
                className="btn ghost sm"
                type="button"
                data-testid="kb-select"
                onClick={() => setKbOpen(true)}
              >
                <Icon name="book" />
                知识库 {conversation.kb_ids?.length ?? 0}
              </button>
              <button
                className="btn ghost sm"
                type="button"
                data-testid="context-open"
                onClick={() => setContextOpen(true)}
              >
                <Icon name="context" />
                上下文
              </button>
              <button className="btn ghost sm" type="button" data-testid="rename" onClick={openRename}>
                <Icon name="edit" />
                重命名
              </button>
              <button
                className="btn ghost sm"
                type="button"
                data-testid="archive"
                onClick={toggleArchive}
                disabled={archiveConversation.isPending}
              >
                <Icon name="archive" />
                {isArchived ? "取消归档" : "归档"}
              </button>
              <button
                className="btn danger sm"
                type="button"
                data-testid="delete"
                onClick={() => setDeleteOpen(true)}
              >
                <Icon name="trash" />
                删除
              </button>
            </div>
          </div>

          {messagesQuery.hasNextPage ? (
            <div className="load-older">
              <button
                className="btn ghost sm"
                type="button"
                onClick={() => void loadOlder()}
                disabled={messagesQuery.isFetchingNextPage}
              >
                {messagesQuery.isFetchingNextPage ? "加载中…" : "加载更早的消息"}
              </button>
            </div>
          ) : null}

          {messagesQuery.isPending ? <div className="chat-empty">正在加载消息…</div> : null}

          {messagesQuery.isError ? (
            <div className="panel">
              <div className="panel-head">
                <div>
                  <h2>消息加载失败</h2>
                  <p>{errorMessage(messagesQuery.error)}</p>
                </div>
              </div>
              <button
                className="btn ghost sm"
                type="button"
                onClick={() => void messagesQuery.refetch()}
              >
                <Icon name="refresh" />
                重试
              </button>
            </div>
          ) : null}

          {!messagesQuery.isPending && !messagesQuery.isError && messages.length === 0 && !liveVisible ? (
            <div className="chat-empty">
              <div className="glyph">
                <Icon name="spark" size={22} />
              </div>
              <h2>这个会话还没有消息</h2>
              <p>在下方输入问题，回答会逐字呈现；停止后会保留已经生成的内容。</p>
            </div>
          ) : null}

          {messages.map((message) => (
            <MessageItem
              key={message.id}
              message={message}
              onCitation={handleCitation}
              onDelete={setMessageToDelete}
              onRegenerate={handleRegenerate}
            />
          ))}

          {liveVisible ? <LiveTurn turn={turn} onCitation={handleCitation} /> : null}

          {turn.phase === "error" && turn.error ? (
            <StreamErrorCard error={turn.error} />
          ) : null}

          {turn.persistError ? (
            <div className="stream-warning" data-testid="persist-warning">
              回答已生成，但台账写入失败（{turn.persistError}）
            </div>
          ) : null}
        </div>
      </div>

      <div className="composer">
        <div className="composer-box">
          <textarea
            rows={1}
            data-testid="composer"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                handleSend();
              }
            }}
            disabled={isArchived}
            placeholder={
              isArchived
                ? "会话已归档；取消归档后才能继续"
                : rateLimitRemaining > 0
                  ? `请求过快，约 ${rateLimitRemaining}s 后恢复`
                  : isStreaming
                    ? "正在生成…可点右侧停止"
                    : "输入问题，Enter 发送，Shift+Enter 换行"
            }
            aria-label="对话输入"
          />
          <div className="composer-toolbar">
            <button
              className={`toggle${useRag ? " on" : ""}`}
              type="button"
              onClick={() => setUseRag((value) => !value)}
              disabled={isArchived}
            >
              资料
            </button>
            <button
              className={`toggle${useMemory ? " on" : ""}`}
              type="button"
              onClick={() => setUseMemory((value) => !value)}
              disabled={isArchived}
            >
              记忆
            </button>
            <button
              className={`toggle${useTools ? " on" : ""}`}
              type="button"
              onClick={() => setUseTools((value) => !value)}
              disabled={isArchived}
            >
              工具
            </button>
            <button
              className={`toggle${attachmentDocIDs.length ? " on" : ""}`}
              type="button"
              data-testid="attachment-open"
              onClick={() => {
                if (!attachmentKbId) {
                  toast("先在知识库选择资料库，才能引用文档附件", "warn");
                  return;
                }
                setAttachmentOpen(true);
              }}
              disabled={isArchived}
              title={attachmentKbId ? "引用已入库文档作为本轮附件" : "当前会话没有资料库"}
            >
              附件{attachmentDocIDs.length ? ` ${attachmentDocIDs.length}` : ""}
            </button>
            <span className="chip">{conversation.model || "默认模型"}</span>
            {rateLimitInfo.remaining !== null && rateLimitRemaining <= 0 ? (
              <span className="chip" data-testid="rate-limit-remaining">
                剩余 {rateLimitInfo.remaining} 次
              </span>
            ) : null}
            <div className="composer-right">
              <span className="quota-hint">
                {rateLimitRemaining > 0
                  ? `限流 · ${rateLimitRemaining}s`
                  : isStreaming
                    ? "正在生成，可随时停止"
                    : "流式对话"}
              </span>
              <button
                className={`send-btn${isStreaming ? " stop" : ""}`}
                type="button"
                data-testid={isStreaming ? "stop" : "send"}
                onClick={() => {
                  if (isStreaming) stop();
                  else handleSend();
                }}
                disabled={
                  isArchived ||
                  (!isStreaming && (rateLimitRemaining > 0 || rateLimitInfo.remaining === 0))
                }
                aria-label={isStreaming ? "停止生成" : "发送"}
              >
                <Icon name={isStreaming ? "stop" : "send"} />
              </button>
            </div>
          </div>
        </div>
        <div className="composer-foot">
          <div className="left">
            <kbd>Enter</kbd> 发送 <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行
          </div>
          <div>回答由模型生成，重要信息请核对引用</div>
        </div>
      </div>

      {kbOpen ? (
        <KbSelectModal conversation={conversation} onClose={() => setKbOpen(false)} />
      ) : null}

      {contextOpen ? (
        <ContextModal
          conversationId={conversation.id}
          onClose={() => setContextOpen(false)}
        />
      ) : null}

      {attachmentOpen ? (
        <Modal
          title="选择文档附件"
          subtitle="只引用当前会话第一个资料库中已入库的文档；本轮参与检索，不改资料库范围。最多 5 个。"
          onClose={() => setAttachmentOpen(false)}
          footer={
            <button
              className="btn primary sm"
              type="button"
              data-testid="attachment-done"
              onClick={() => setAttachmentOpen(false)}
            >
              完成
            </button>
          }
        >
          {attachmentDocsQuery.isPending ? <p className="muted">正在加载文档…</p> : null}
          {attachmentDocsQuery.isError ? (
            <p className="muted">{errorMessage(attachmentDocsQuery.error)}</p>
          ) : null}
          {!attachmentDocsQuery.isPending && attachmentDocs.length === 0 ? (
            <p className="muted">当前资料库还没有已入库文档，请先在知识库上传。</p>
          ) : null}
          <div className="attachment-list">
            {attachmentDocs.map((doc) => {
              const checked = attachmentDocIDs.includes(doc.id);
              return (
                <label className="attachment-option" key={doc.id}>
                  <input
                    type="checkbox"
                    checked={checked}
                    data-testid="attachment-doc"
                    onChange={() => {
                      setAttachmentDocIDs((current) => {
                        if (current.includes(doc.id)) {
                          return current.filter((id) => id !== doc.id);
                        }
                        if (current.length >= 5) {
                          toast("最多选择 5 个附件", "warn");
                          return current;
                        }
                        return [...current, doc.id];
                      });
                    }}
                  />
                  <span>
                    <b>{doc.doc_name}</b>
                    <small>{doc.chunk_count} 个切片</small>
                  </span>
                </label>
              );
            })}
          </div>
        </Modal>
      ) : null}

      {messageToDelete ? (
        <Modal
          title="删除这条消息？"
          subtitle="会同时从 AI 短期上下文删除；长期记忆不会自动清除。"
          onClose={() => setMessageToDelete(null)}
          footer={
            <>
              <button
                className="btn ghost sm"
                type="button"
                onClick={() => setMessageToDelete(null)}
              >
                取消
              </button>
              <button
                className="btn danger sm"
                type="button"
                data-testid="delete-message-confirm"
                onClick={() => {
                  deleteMessageMutation.mutate(messageToDelete.id, {
                    onSuccess: () => {
                      setMessageToDelete(null);
                      toast("消息已删除，AI 上下文已同步", "ok");
                    },
                    onError: (error) => toast(errorMessage(error), "bad"),
                  });
                }}
                disabled={deleteMessageMutation.isPending}
              >
                确认删除
              </button>
            </>
          }
        >
          <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.7 }}>
            将删除{messageToDelete.role === "user" ? "用户" : "助手"}消息：
            「{messageToDelete.content.slice(0, 80)}
            {messageToDelete.content.length > 80 ? "…" : ""}」
          </p>
        </Modal>
      ) : null}

      {renameOpen ? (
        <Modal
          title="重命名会话"
          subtitle="手动标题不会再被自动标题覆盖。"
          onClose={() => setRenameOpen(false)}
          footer={
            <>
              <button className="btn ghost sm" type="button" onClick={() => setRenameOpen(false)}>
                取消
              </button>
              <button
                className="btn primary sm"
                type="button"
                data-testid="rename-save"
                onClick={saveRename}
                disabled={updateConversation.isPending}
              >
                保存
              </button>
            </>
          }
        >
          <label className="field">
            <span>标题</span>
            <input
              data-testid="rename-input"
              value={titleDraft}
              maxLength={100}
              onChange={(event) => setTitleDraft(event.target.value)}
              autoFocus
            />
          </label>
        </Modal>
      ) : null}

      {deleteOpen ? (
        <Modal
          title="删除会话？"
          subtitle="删除后消息台账不再可查；这个操作不可撤销。"
          onClose={() => setDeleteOpen(false)}
          footer={
            <>
              <button className="btn ghost sm" type="button" onClick={() => setDeleteOpen(false)}>
                取消
              </button>
              <button
                className="btn danger sm"
                type="button"
                data-testid="delete-confirm"
                onClick={confirmDelete}
                disabled={deleteConversation.isPending}
              >
                确认删除
              </button>
            </>
          }
        >
          <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.7 }}>
            即将删除「{conversation.title || "未命名对话"}」及其消息。若只是暂时不用，建议归档。
          </p>
        </Modal>
      ) : null}
    </section>
  );
}
