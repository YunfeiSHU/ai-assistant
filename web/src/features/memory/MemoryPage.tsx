import { useEffect, useMemo, useState } from "react";

import { isApiError } from "@/shared/api/error";
import { formatDateTime, formatRelative } from "@/shared/lib/format";
import { Icon } from "@/shared/ui/Icon";
import { Modal } from "@/shared/ui/Modal";
import { toast } from "@/shared/ui/toast";

import {
  useClearMemories,
  useCreateMemory,
  useDeleteMemory,
  useMemories,
  useMemorySettings,
  useUpdateMemory,
  useUpdateMemorySettings,
} from "./queries";
import type { Memory, MemoryKind } from "./types";

function errorText(error: unknown): string {
  if (isApiError(error)) {
    if (error.code === "MEMORY_DISABLED") return "记忆已关闭，先在右上角开启";
    return `${error.code} · ${error.message}`;
  }
  return "请求失败，请稍后重试";
}

function flatten(pages: { items: Memory[] }[] | undefined): Memory[] {
  const seen = new Set<string>();
  const items: Memory[] = [];
  for (const page of pages ?? []) {
    for (const memory of page.items) {
      if (seen.has(memory.id)) continue;
      seen.add(memory.id);
      items.push(memory);
    }
  }
  return items;
}

const KIND_LABEL: Record<string, string> = {
  preference: "偏好",
  fact: "事实",
};

export function MemoryPage() {
  const settingsQuery = useMemorySettings();
  const [enabled, setEnabled] = useState(true);
  const [topN, setTopN] = useState(3);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [kind, setKind] = useState<"" | MemoryKind>("");

  useEffect(() => {
    if (!settingsQuery.data || settingsDirty) return;
    setEnabled(settingsQuery.data.memory_enabled);
    setTopN(settingsQuery.data.memory_top_n);
  }, [settingsQuery.data, settingsDirty]);

  const memoriesQuery = useMemories(kind, enabled);
  const memories = useMemo(() => flatten(memoriesQuery.data?.pages), [memoriesQuery.data]);

  const updateSettings = useUpdateMemorySettings();
  const createMemory = useCreateMemory();
  const updateMemory = useUpdateMemory();
  const deleteMemory = useDeleteMemory();
  const clearMemories = useClearMemories();

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<Memory | null>(null);
  const [content, setContent] = useState("");
  const [editorKind, setEditorKind] = useState<MemoryKind>("fact");
  const [confidence, setConfidence] = useState(1);
  const [clearOpen, setClearOpen] = useState(false);

  function openCreate() {
    setEditing(null);
    setContent("");
    setEditorKind("fact");
    setConfidence(1);
    setEditorOpen(true);
  }

  function openEdit(memory: Memory) {
    setEditing(memory);
    setContent(memory.content);
    setEditorKind(memory.kind === "preference" ? "preference" : "fact");
    setConfidence(memory.confidence);
    setEditorOpen(true);
  }

  function saveMemory() {
    const text = content.trim();
    if (!text) {
      toast("记忆内容不能为空", "warn");
      return;
    }
    if (editing) {
      updateMemory.mutate(
        {
          id: editing.id,
          input: { content: text, kind: editorKind },
        },
        {
          onSuccess: () => {
            setEditorOpen(false);
            toast("记忆已更新", "ok");
          },
          onError: (error) => toast(errorText(error), "bad"),
        },
      );
      return;
    }
    createMemory.mutate(
      { content: text, kind: editorKind, confidence },
      {
        onSuccess: () => {
          setEditorOpen(false);
          toast("记忆已添加", "ok");
        },
        onError: (error) => toast(errorText(error), "bad"),
      },
    );
  }

  function saveSettings() {
    updateSettings.mutate(
      { memory_enabled: enabled, memory_top_n: topN },
      {
        onSuccess: () => {
          setSettingsDirty(false);
          toast("记忆设置已保存", "ok");
        },
        onError: (error) => toast(errorText(error), "bad"),
      },
    );
  }

  const memoryDisabled = !enabled;
  const settings = settingsQuery.data;

  return (
    <section className="page">
      <div className="page-inner">
        <div className="panel">
          <div className="panel-head">
            <div>
              <h2>长期记忆</h2>
              <p>偏好与事实分开保存；命中数、置信度和过期时间都可核对。</p>
            </div>
            <div className="right memory-settings">
              <label className="switch" title="开启/关闭长期记忆">
                <input
                  type="checkbox"
                  data-testid="memory-enabled"
                  checked={enabled}
                  onChange={(event) => {
                    setEnabled(event.target.checked);
                    setSettingsDirty(true);
                  }}
                />
                <span />
              </label>
              <span className="search-meta">记忆</span>
              <label className="search-meta" htmlFor="memory-topn">
                每次召回
              </label>
              <input
                id="memory-topn"
                className="inline-number"
                type="number"
                min={1}
                max={20}
                value={topN}
                data-testid="memory-topn"
                onChange={(event) => {
                  setTopN(Number(event.target.value));
                  setSettingsDirty(true);
                }}
              />
              <button
                className="btn ghost sm"
                type="button"
                data-testid="memory-settings-save"
                onClick={saveSettings}
                disabled={updateSettings.isPending}
              >
                保存设置
              </button>
              <button
                className="btn primary sm"
                type="button"
                data-testid="memory-add"
                onClick={openCreate}
                disabled={memoryDisabled}
              >
                <Icon name="plus" />
                添加记忆
              </button>
              <button
                className="btn danger sm"
                type="button"
                data-testid="memory-clear"
                onClick={() => setClearOpen(true)}
                disabled={memoryDisabled || memories.length === 0}
              >
                <Icon name="trash" />
                清空
              </button>
            </div>
          </div>

          {settings?.cleared_at ? (
            <div className="search-meta">上次清空：{formatDateTime(settings.cleared_at)}</div>
          ) : null}

          {memoryDisabled ? (
            <div className="stream-warning" data-testid="memory-disabled-banner">
              长期记忆已关闭。手动记忆仍保留；模型不会在对话中召回或写入。
            </div>
          ) : null}

          <div className="filter-bar" style={{ marginTop: 12 }}>
            {[
              ["", "全部"],
              ["preference", "偏好"],
              ["fact", "事实"],
            ].map(([key, label]) => (
              <button
                className={`filter${kind === key ? " active" : ""}`}
                type="button"
                key={key}
                data-testid="memory-filter"
                onClick={() => setKind(key as "" | MemoryKind)}
              >
                {label}
              </button>
            ))}
            <span className="search-meta" style={{ marginLeft: "auto" }}>
              已加载 {memories.length} 条
            </span>
          </div>

          {memoriesQuery.isPending && enabled ? (
            <div className="table-empty">正在加载记忆…</div>
          ) : null}
          {memoriesQuery.isError ? (
            <div className="table-empty">
              记忆加载失败：{errorText(memoriesQuery.error)}{" "}
              <button className="btn ghost sm" type="button" onClick={() => void memoriesQuery.refetch()}>
                重试
              </button>
            </div>
          ) : null}
          {!memoryDisabled && !memoriesQuery.isPending && !memoriesQuery.isError && memories.length === 0 ? (
            <div className="table-empty">还没有记忆；对话中模型会自动抽取，也可以手动添加。</div>
          ) : null}

          {!memoryDisabled ? (
            <div className="memory-grid">
              {memories.map((memory) => (
                <div className="memory-card" data-testid="memory-item" key={memory.id}>
                  <div className="top">
                    <span className={`chip${memory.kind === "preference" ? " accent" : ""}`}>
                      {KIND_LABEL[memory.kind] ?? memory.kind}
                    </span>
                    {memory.expired ? <span className="chip bad">已过期</span> : null}
                    <span className="search-meta" style={{ marginLeft: "auto" }}>
                      命中 {memory.hit_count}
                    </span>
                    <button
                      className="icon-btn"
                      type="button"
                      data-testid="memory-edit"
                      title="编辑"
                      onClick={() => openEdit(memory)}
                    >
                      <Icon name="edit" />
                    </button>
                    <button
                      className="icon-btn"
                      type="button"
                      data-testid="memory-delete"
                      title="删除"
                      onClick={() => {
                        deleteMemory.mutate(memory.id, {
                          onSuccess: () => toast("记忆已删除", "ok"),
                          onError: (error) => toast(errorText(error), "bad"),
                        });
                      }}
                    >
                      <Icon name="trash" />
                    </button>
                  </div>
                  <div className="content">{memory.content}</div>
                  <div className="foot">
                    <span>置信 {(memory.confidence * 100).toFixed(0)}%</span>
                    <span>{formatRelative(memory.created_at)}</span>
                    {memory.expires_at ? <span>到期 {formatDateTime(memory.expires_at)}</span> : null}
                    <div className="bar">
                      <i style={{ width: `${Math.round(memory.confidence * 100)}%` }} />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : null}

          {memoriesQuery.hasNextPage ? (
            <button
              className="btn ghost sm"
              type="button"
              onClick={() => void memoriesQuery.fetchNextPage()}
              disabled={memoriesQuery.isFetchingNextPage}
            >
              {memoriesQuery.isFetchingNextPage ? "加载中…" : "加载更多记忆"}
            </button>
          ) : null}
        </div>
      </div>

      {editorOpen ? (
        <Modal
          title={editing ? "编辑记忆" : "添加记忆"}
          subtitle="手动记忆会以 manual 来源写入；重复内容会累加命中数。"
          onClose={() => setEditorOpen(false)}
          footer={
            <>
              <button className="btn ghost sm" type="button" onClick={() => setEditorOpen(false)}>
                取消
              </button>
              <button
                className="btn primary sm"
                type="button"
                data-testid="memory-save"
                onClick={saveMemory}
                disabled={createMemory.isPending || updateMemory.isPending}
              >
                保存
              </button>
            </>
          }
        >
          <label className="field">
            <span>内容</span>
            <textarea
              data-testid="memory-content"
              value={content}
              maxLength={500}
              onChange={(event) => setContent(event.target.value)}
              autoFocus
            />
          </label>
          <div className="form-grid">
            <label className="field">
              <span>类型</span>
              <select
                data-testid="memory-kind"
                value={editorKind}
                onChange={(event) => setEditorKind(event.target.value as MemoryKind)}
              >
                <option value="fact">事实</option>
                <option value="preference">偏好</option>
              </select>
            </label>
            <label className="field">
              <span>置信度</span>
              <input
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={confidence}
                disabled={Boolean(editing)}
                onChange={(event) => setConfidence(Number(event.target.value))}
              />
            </label>
          </div>
        </Modal>
      ) : null}

      {clearOpen ? (
        <Modal
          title="清空全部记忆？"
          subtitle="会删除所有手动与自动记忆；这个操作不可撤销。"
          onClose={() => setClearOpen(false)}
          footer={
            <>
              <button className="btn ghost sm" type="button" onClick={() => setClearOpen(false)}>
                取消
              </button>
              <button
                className="btn danger sm"
                type="button"
                data-testid="delete-confirm"
                onClick={() => {
                  clearMemories.mutate(undefined, {
                    onSuccess: () => {
                      setClearOpen(false);
                      toast("全部记忆已清空", "ok");
                    },
                    onError: (error) => toast(errorText(error), "bad"),
                  });
                }}
                disabled={clearMemories.isPending}
              >
                确认清空
              </button>
            </>
          }
        >
          <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.7 }}>
            当前已加载 {memories.length} 条记忆。清空只影响长期记忆，不删除会话消息。
          </p>
        </Modal>
      ) : null}
    </section>
  );
}
