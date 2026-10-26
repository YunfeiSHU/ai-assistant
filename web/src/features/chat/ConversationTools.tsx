import { useState } from "react";

import { useKnowledgeBases } from "@/features/knowledge/queries";
import type { Conversation } from "@/features/conversations/types";
import { useUpdateConversation } from "@/features/conversations/queries";
import { errorText } from "@/shared/api/error";
import { formatDateTime, formatNumber } from "@/shared/lib/format";
import { Icon } from "@/shared/ui/Icon";
import { Modal } from "@/shared/ui/Modal";
import { toast } from "@/shared/ui/toast";

import {
  useClearConversationContext,
  useConversationContext,
  useConversationSummary,
  useRebuildConversationSummary,
} from "@/features/memory/queries";

export function KbSelectModal({
  conversation,
  onClose,
}: {
  conversation: Conversation;
  onClose: () => void;
}) {
  const kbsQuery = useKnowledgeBases({ limit: 50 });
  const update = useUpdateConversation();
  const [selected, setSelected] = useState<string[]>(conversation.kb_ids ?? []);

  const kbs = kbsQuery.data?.pages.flatMap((page) => page.items) ?? [];

  function toggle(id: string) {
    setSelected((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id);
      if (current.length >= 10) {
        toast("单会话最多关联 10 个知识库", "warn");
        return current;
      }
      return [...current, id];
    });
  }

  function save() {
    update.mutate(
      { id: conversation.id, patch: { kb_ids: selected } },
      {
        onSuccess: () => {
          toast("知识库关联已保存", "ok");
          onClose();
        },
        onError: (error) => toast(errorText(error), "bad"),
      },
    );
  }

  return (
    <Modal
      title="关联知识库"
      subtitle="发送消息时，网关会把选中的 KB 作为 RAG 范围；最多 10 个。"
      onClose={onClose}
      footer={
        <>
          <span className="search-meta">已选 {selected.length} 个</span>
          <button className="btn ghost sm" type="button" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary sm"
            type="button"
            data-testid="kb-select-save"
            onClick={save}
            disabled={update.isPending}
          >
            保存
          </button>
        </>
      }
    >
      {kbsQuery.isPending ? <div className="table-empty">正在加载知识库…</div> : null}
      {kbsQuery.isError ? (
        <div className="table-empty">知识库加载失败：{errorText(kbsQuery.error)}</div>
      ) : null}
      {!kbsQuery.isPending && kbs.length === 0 ? (
        <div className="table-empty">还没有知识库；先去知识库页创建并入库。</div>
      ) : null}
      <div className="kb-picker">
        {kbs.map((kb) => (
          <label className="kb-option" data-testid="kb-option" key={kb.id}>
            <input
              type="checkbox"
              checked={selected.includes(kb.id)}
              onChange={() => toggle(kb.id)}
            />
            <span>
              <b>{kb.name}</b>
              <small>
                {kb.document_count} 文档 · {kb.chunk_count} 切片
              </small>
            </span>
          </label>
        ))}
      </div>
    </Modal>
  );
}

export function ContextModal({
  conversationId,
  onClose,
}: {
  conversationId: string;
  onClose: () => void;
}) {
  const contextQuery = useConversationContext(conversationId, true);
  const summaryExists = Boolean(contextQuery.data?.summary.exists);
  const summaryQuery = useConversationSummary(conversationId, summaryExists);
  const clearContext = useClearConversationContext(conversationId);
  const rebuildSummary = useRebuildConversationSummary(conversationId);
  const [clearConfirm, setClearConfirm] = useState(false);

  const context = contextQuery.data;
  const budget = context?.budget;
  const ratio =
    budget && budget.context_token_budget > 0
      ? Math.min(100, Math.round((budget.total_tokens / budget.context_token_budget) * 100))
      : 0;

  function handleClear() {
    clearContext.mutate(undefined, {
      onSuccess: () => {
        setClearConfirm(false);
        toast("上下文已清空（消息与长期记忆未删除）", "ok");
      },
      onError: (error) => toast(errorText(error), "bad"),
    });
  }

  function handleRebuild() {
    rebuildSummary.mutate(undefined, {
      onSuccess: (result) => toast(`摘要重建已入队：${result.task_id}`, "ok"),
      onError: (error) => toast(errorText(error), "bad"),
    });
  }

  return (
    <Modal
      title="上下文透视"
      subtitle="这里能看到模型实际拿到的预算与裁剪，而不是猜。"
      onClose={onClose}
      footer={
        clearConfirm ? (
          <>
            <span className="search-meta">清空只影响本会话上下文，不删除消息/长期记忆。</span>
            <button className="btn ghost sm" type="button" onClick={() => setClearConfirm(false)}>
              取消
            </button>
            <button
              className="btn danger sm"
              type="button"
              data-testid="context-clear-confirm"
              onClick={handleClear}
              disabled={clearContext.isPending}
            >
              确认清空
            </button>
          </>
        ) : (
          <>
            <button
              className="btn danger sm"
              type="button"
              data-testid="context-clear"
              onClick={() => setClearConfirm(true)}
            >
              清空上下文
            </button>
            <button
              className="btn ghost sm"
              type="button"
              data-testid="summary-rebuild"
              onClick={handleRebuild}
              disabled={rebuildSummary.isPending}
            >
              <Icon name="refresh" />
              重建摘要
            </button>
            <button className="btn ghost sm" type="button" onClick={onClose}>
              关闭
            </button>
          </>
        )
      }
    >
      {contextQuery.isPending ? <div className="table-empty">正在读取上下文…</div> : null}
      {contextQuery.isError ? (
        <div className="table-empty">上下文读取失败：{errorText(contextQuery.error)}</div>
      ) : null}

      {context ? (
        <div className="context-panel">
          <div className="context-budget">
            <div className="context-budget-head">
              <b>Token 预算</b>
              <span className="mono">
                {formatNumber(budget?.total_tokens ?? 0)} /{" "}
                {formatNumber(budget?.context_token_budget ?? 0)}
              </span>
            </div>
            <div className="task-progress">
              <div className="track">
                <i style={{ width: `${ratio}%` }} />
              </div>
              <span>{ratio}%</span>
            </div>
            <div className="search-meta">
              消息 {context.message_count} 条 · 摘要{" "}
              {context.summary.exists
                ? `已生成（${context.summary.token_count} tokens）`
                : "尚未生成"}
            </div>
          </div>

          <div className="context-columns">
            <div>
              <div className="section-label">已占用</div>
              {Object.keys(budget?.used ?? {}).length === 0 ? (
                <div className="search-meta">暂无</div>
              ) : (
                Object.entries(budget?.used ?? {}).map(([part, tokens]) => (
                  <div className="kv" key={part}>
                    <span>{part}</span>
                    <b>{formatNumber(tokens)}</b>
                  </div>
                ))
              )}
            </div>
            <div>
              <div className="section-label">被裁剪</div>
              {Object.keys(budget?.trimmed ?? {}).length === 0 ? (
                <div className="search-meta">无</div>
              ) : (
                Object.entries(budget?.trimmed ?? {}).map(([part, tokens]) => (
                  <div className="kv" key={part}>
                    <span>{part}</span>
                    <b>{formatNumber(tokens)}</b>
                  </div>
                ))
              )}
            </div>
          </div>

          <div>
            <div className="section-label">摘要</div>
            {summaryExists ? (
              summaryQuery.isPending ? (
                <div className="search-meta">正在加载摘要…</div>
              ) : summaryQuery.isError ? (
                <div className="search-meta">摘要读取失败：{errorText(summaryQuery.error)}</div>
              ) : (
                <>
                  <div className="context-summary">{summaryQuery.data?.content}</div>
                  <div className="search-meta">
                    覆盖到 {summaryQuery.data?.covered_until || "—"} · 来源{" "}
                    {summaryQuery.data?.source_message_count ?? 0} 条
                  </div>
                </>
              )
            ) : (
              <div className="search-meta">还没有摘要；可点“重建摘要”生成。</div>
            )}
          </div>

          <div>
            <div className="section-label">上下文消息</div>
            <div className="context-messages">
              {context.messages.length === 0 ? (
                <div className="search-meta">上下文为空。</div>
              ) : (
                context.messages.map((message) => (
                  <div className="context-message" key={message.message_id}>
                    <div className="context-message-head">
                      <span className="mono">{message.role}</span>
                      <span>{message.tokens} tokens</span>
                      {message.partial ? <span className="chip warn">partial</span> : null}
                      <span className="search-meta" style={{ marginLeft: "auto" }}>
                        {formatDateTime(message.created_at)}
                      </span>
                    </div>
                    <div>{message.content}</div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
