import { useEffect, useMemo, useRef, useState } from "react";

import { useNavigate } from "@tanstack/react-router";

import type { Task } from "@/features/tasks/types";
import { useTasks } from "@/features/tasks/queries";
import { isApiError } from "@/shared/api/error";
import { formatBytes, formatNumber } from "@/shared/lib/format";
import { Icon } from "@/shared/ui/Icon";
import { Modal } from "@/shared/ui/Modal";
import { toast } from "@/shared/ui/toast";

import {
  useChunks,
  useCreateKnowledgeBase,
  useDeleteDocument,
  useDeleteKnowledgeBase,
  useDocuments,
  useKnowledgeBases,
  useSearchKnowledgeBase,
  useUploadDocument,
} from "./queries";
import type {
  DocumentChunk,
  KnowledgeBase,
  KnowledgeDocument,
  SearchItem,
} from "./types";

function errorText(error: unknown): string {
  if (isApiError(error)) return `${error.code} · ${error.message}`;
  return "请求失败，请稍后重试";
}

function statusTone(status: string): string {
  if (["INDEXED", "SUCCEEDED"].includes(status)) return "ok";
  if (["FAILED", "CANCELED"].includes(status)) return "bad";
  if (["PENDING", "QUEUED", "PARSING", "CHUNKING", "EMBEDDING", "RUNNING", "DELETING"].includes(status)) {
    return "warn";
  }
  return "";
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    PENDING: "待处理",
    QUEUED: "排队",
    PARSING: "解析中",
    CHUNKING: "切片中",
    EMBEDDING: "向量化",
    INDEXED: "已入库",
    RUNNING: "运行中",
    SUCCEEDED: "已完成",
    FAILED: "失败",
    CANCELED: "已取消",
    DELETING: "删除中",
    DELETED: "已删除",
  };
  return labels[status] ?? status;
}

function taskTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    document_ingest: "文档入库",
    document_delete: "文档删除",
    kb_reindex: "知识库重建",
    summary_build: "摘要生成",
    memory_extract: "记忆抽取",
  };
  return labels[type] ?? type;
}

function flatten<T>(pages: { items: T[] }[] | undefined): T[] {
  const seen = new Set<string>();
  const items: T[] = [];
  for (const page of pages ?? []) {
    for (const item of page.items as (T & { id?: string; chunk_id?: string })[]) {
      const key = item.id ?? item.chunk_id ?? JSON.stringify(item);
      if (seen.has(key)) continue;
      seen.add(key);
      items.push(item);
    }
  }
  return items;
}

function scoreText(item: SearchItem): string {
  const vector = item.vector_score.toFixed(2);
  const rerank = item.rerank_score == null ? null : item.rerank_score.toFixed(2);
  return rerank ? `vec ${vector} / rerank ${rerank}` : `vec ${vector}`;
}

export function KnowledgePage() {
  const navigate = useNavigate();

  const kbQuery = useKnowledgeBases({ limit: 50 });
  const knowledgeBases = useMemo(
    () => flatten<KnowledgeBase>(kbQuery.data?.pages),
    [kbQuery.data],
  );

  const [activeKbId, setActiveKbId] = useState<string | null>(null);
  const activeKb = knowledgeBases.find((kb) => kb.id === activeKbId) ?? knowledgeBases[0] ?? null;

  useEffect(() => {
    if (!knowledgeBases.length) {
      if (activeKbId) setActiveKbId(null);
      return;
    }
    if (!activeKbId || !knowledgeBases.some((kb) => kb.id === activeKbId)) {
      setActiveKbId(knowledgeBases[0].id);
    }
  }, [knowledgeBases, activeKbId]);

  const docsQuery = useDocuments(activeKb?.id);
  const documents = useMemo(
    () => flatten<KnowledgeDocument>(docsQuery.data?.pages),
    [docsQuery.data],
  );

  const tasksQuery = useTasks({ limit: 10 });
  const tasks = useMemo(() => {
    const docIds = new Set(documents.map((document) => document.id));
    return flatten<Task>(tasksQuery.data?.pages).filter(
      (task) =>
        docIds.has(task.resource_id) ||
        (task.resource_type === "knowledge_base" && task.resource_id === activeKb?.id),
    );
  }, [tasksQuery.data, documents, activeKb?.id]);

  const [chunkDoc, setChunkDoc] = useState<KnowledgeDocument | null>(null);
  const chunksQuery = useChunks(chunkDoc?.id, Boolean(chunkDoc));
  const chunks = useMemo(() => flatten<DocumentChunk>(chunksQuery.data?.pages), [chunksQuery.data]);

  const createKb = useCreateKnowledgeBase();
  const deleteKb = useDeleteKnowledgeBase();
  const deleteDoc = useDeleteDocument(activeKb?.id);
  const uploadDoc = useUploadDocument(activeKb?.id);
  const search = useSearchKnowledgeBase();

  useEffect(() => {
    search.reset();
  }, [activeKb?.id]);

  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [chunkSize, setChunkSize] = useState(512);
  const [chunkOverlap, setChunkOverlap] = useState(64);
  const [topK, setTopK] = useState(20);
  const [rerankTopN, setRerankTopN] = useState(5);

  const [deleteKbOpen, setDeleteKbOpen] = useState(false);
  const [docToDelete, setDocToDelete] = useState<KnowledgeDocument | null>(null);

  const [query, setQuery] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [uploadName, setUploadName] = useState("");
  const [uploadPercent, setUploadPercent] = useState(0);
  const [uploadState, setUploadState] = useState<"idle" | "uploading" | "done" | "error">("idle");

  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadedDoc = documents.find((document) => document.doc_name === uploadName);

  function resetCreateForm() {
    setName("");
    setDescription("");
    setChunkSize(512);
    setChunkOverlap(64);
    setTopK(20);
    setRerankTopN(5);
  }

  function submitCreate() {
    const trimmed = name.trim();
    if (!trimmed) {
      toast("知识库名称不能为空", "warn");
      return;
    }
    if (chunkOverlap >= chunkSize) {
      toast("重叠长度必须小于切片长度", "warn");
      return;
    }
    if (rerankTopN > topK) {
      toast("重排保留数必须 ≤ 召回数", "warn");
      return;
    }
    createKb.mutate(
      {
        name: trimmed,
        description: description.trim(),
        chunk_size: chunkSize,
        chunk_overlap: chunkOverlap,
        retrieval_top_k: topK,
        rerank_top_n: rerankTopN,
      },
      {
        onSuccess: (kb) => {
          setActiveKbId(kb.id);
          setCreateOpen(false);
          resetCreateForm();
          toast("知识库已创建", "ok");
        },
        onError: (error) => toast(errorText(error), "bad"),
      },
    );
  }

  function handleFile(file: File | null) {
    if (!file || !activeKb || uploadDoc.isPending) return;
    if (file.size > 50 * 1024 * 1024) {
      toast("单文件不能超过 50MB", "warn");
      return;
    }
    setUploadName(file.name);
    setUploadPercent(0);
    setUploadState("uploading");
    uploadDoc.mutate(
      {
        file,
        onProgress: (percent) => setUploadPercent(percent),
      },
      {
        onSuccess: (accepted) => {
          setUploadPercent(100);
          setUploadState("done");
          toast(
            accepted.duplicated ? "文件哈希重复，已复用现有文档" : "上传完成，已进入入库队列",
            "ok",
          );
        },
        onError: (error) => {
          setUploadState("error");
          toast(errorText(error), "bad");
        },
      },
    );
  }

  function submitSearch() {
    if (!activeKb) return;
    const trimmed = query.trim();
    if (!trimmed) {
      toast("先输入检索语句", "warn");
      return;
    }
    search.mutate({
      kbId: activeKb.id,
      request: {
        query: trimmed,
        top_k: activeKb.retrieval_top_k,
        rerank_top_n: activeKb.rerank_top_n,
        with_rerank: true,
      },
    });
  }

  const docsError = docsQuery.isError;
  const noKnowledge = !kbQuery.isPending && knowledgeBases.length === 0;

  return (
    <section className="page">
      <div className="page-inner">
        <div className="kb-grid">
          {knowledgeBases.map((kb) => (
            <button
              className={`kb-card${kb.id === activeKb?.id ? " active" : ""}`}
              key={kb.id}
              type="button"
              data-testid="kb-card"
              onClick={() => setActiveKbId(kb.id)}
            >
              <div className="top">
                <Icon name="book" />
                <span className="name">{kb.name}</span>
                {kb.id === activeKb?.id ? <span className="chip accent">当前</span> : null}
              </div>
              <div className="desc">{kb.description || "暂无描述"}</div>
              <div className="kb-stats">
                <span>
                  <b>{formatNumber(kb.document_count)}</b> 文档
                </span>
                <span>
                  <b>{formatNumber(kb.chunk_count)}</b> 切片
                </span>
                <span className="mono">{kb.embedding_model || "默认 embedding"}</span>
              </div>
            </button>
          ))}

          <button
            className="kb-card kb-new"
            type="button"
            data-testid="kb-create"
            onClick={() => setCreateOpen(true)}
          >
            <Icon name="plus" />
            <span>新建知识库</span>
          </button>
        </div>

        {kbQuery.isPending ? <div className="panel">正在加载知识库…</div> : null}
        {kbQuery.isError ? (
          <div className="panel">
            <div className="panel-head">
              <div>
                <h2>知识库加载失败</h2>
                <p>{errorText(kbQuery.error)}</p>
              </div>
            </div>
            <button className="btn ghost sm" type="button" onClick={() => void kbQuery.refetch()}>
              <Icon name="refresh" />
              重试
            </button>
          </div>
        ) : null}

        {noKnowledge ? (
          <div className="panel">
            <div className="rail-empty">
              <div className="glyph">
                <Icon name="book" size={26} />
              </div>
              <b>还没有知识库</b>
              <p>先建一个知识库，再上传 PDF / Markdown / DOCX / TXT。</p>
              <button className="btn primary sm" type="button" onClick={() => setCreateOpen(true)}>
                <Icon name="plus" />
                新建知识库
              </button>
            </div>
          </div>
        ) : null}

        {activeKb ? (
          <>
            <div className="panel">
              <div className="panel-head">
                <div>
                  <h2>{activeKb.name}</h2>
                  <p>
                    {activeKb.description || "暂无描述"} · 切片 {activeKb.chunk_size} / 重叠{" "}
                    {activeKb.chunk_overlap} · 召回 {activeKb.retrieval_top_k} / 重排{" "}
                    {activeKb.rerank_top_n}
                  </p>
                </div>
                <div className="right kb-head-actions">
                  <button
                    className="btn primary sm"
                    type="button"
                    data-testid="doc-upload-button"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={uploadDoc.isPending}
                  >
                    <Icon name="upload" />
                    {uploadDoc.isPending ? "上传中…" : "上传文档"}
                  </button>
                  <button
                    className="btn ghost sm"
                    type="button"
                    data-testid="kb-delete"
                    onClick={() => setDeleteKbOpen(true)}
                  >
                    <Icon name="trash" />
                    删除知识库
                  </button>
                </div>
              </div>

              <div
                className={`dropzone${dragOver ? " drag" : ""}`}
                role="button"
                tabIndex={0}
                onClick={() => fileInputRef.current?.click()}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    fileInputRef.current?.click();
                  }
                }}
                onDragOver={(event) => {
                  event.preventDefault();
                  setDragOver(true);
                }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(event) => {
                  event.preventDefault();
                  setDragOver(false);
                  handleFile(event.dataTransfer.files?.[0] ?? null);
                }}
              >
                <div className="icon">
                  <Icon name="upload" size={20} />
                </div>
                <div>
                  <b>拖拽文件到这里，或点击选择</b>
                  <div className="sub">
                    支持 PDF / MD / DOCX / TXT · 单文件 ≤ 50MB · 上传不阻塞页面
                  </div>
                </div>
                <input
                  ref={fileInputRef}
                  className="hidden"
                  type="file"
                  accept=".pdf,.md,.markdown,.docx,.txt"
                  data-testid="doc-upload"
                  onChange={(event) => {
                    handleFile(event.target.files?.[0] ?? null);
                    event.target.value = "";
                  }}
                />
              </div>

              {uploadState !== "idle" ? (
                <div className="upload-banner" data-testid="upload-progress">
                  <div className="upload-info">
                    <Icon name="file" />
                    <div>
                      <b>{uploadName}</b>
                      <span>
                        {uploadState === "uploading"
                          ? `正在上传 ${uploadPercent}%`
                          : uploadState === "error"
                            ? "上传失败，可重试"
                            : uploadedDoc?.status === "INDEXED"
                              ? "已入库"
                              : uploadedDoc?.status === "FAILED"
                                ? "入库失败，去任务中心查看原因"
                                : "上传完成，等待入库"}
                      </span>
                    </div>
                  </div>
                  <div className="task-progress">
                    <div className="track">
                      <i style={{ width: `${uploadPercent}%` }} />
                    </div>
                    <span>{uploadPercent}%</span>
                  </div>
                  <button
                    className="icon-btn"
                    type="button"
                    aria-label="关闭上传状态"
                    onClick={() => setUploadState("idle")}
                  >
                    <Icon name="x" />
                  </button>
                </div>
              ) : null}

              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>类型</th>
                      <th>大小</th>
                      <th>状态</th>
                      <th>切片</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {docsQuery.isPending ? (
                      <tr>
                        <td colSpan={6}>
                          <div className="table-empty">正在加载文档…</div>
                        </td>
                      </tr>
                    ) : null}
                    {docsError ? (
                      <tr>
                        <td colSpan={6}>
                          <div className="table-empty">
                            文档加载失败：{errorText(docsQuery.error)}{" "}
                            <button className="btn ghost sm" type="button" onClick={() => void docsQuery.refetch()}>
                              重试
                            </button>
                          </div>
                        </td>
                      </tr>
                    ) : null}
                    {!docsQuery.isPending && !docsError && documents.length === 0 ? (
                      <tr>
                        <td colSpan={6}>
                          <div className="table-empty">还没有文档，先上传一份。</div>
                        </td>
                      </tr>
                    ) : null}
                    {documents.map((document) => (
                      <tr key={document.id} data-testid="doc-item">
                        <td className="name">
                          <span className="doc-name">{document.doc_name}</span>
                          {document.truncated ? <span className="chip warn">已截断</span> : null}
                        </td>
                        <td className="mono">{document.file_ext || "—"}</td>
                        <td>{formatBytes(document.size_bytes)}</td>
                        <td>
                          <span className={`chip ${statusTone(document.status)}`} data-testid="doc-status">
                            {statusLabel(document.status)}
                          </span>
                          {document.error ? (
                            <div className="doc-error" title={document.error.code}>
                              {document.error.code}
                            </div>
                          ) : null}
                        </td>
                        <td>{document.chunk_count || "—"}</td>
                        <td>
                          <div className="actions">
                            <button
                              className="icon-btn"
                              type="button"
                              title="查看切片"
                              data-testid="doc-chunks"
                              onClick={() => setChunkDoc(document)}
                            >
                              <Icon name="context" />
                            </button>
                            <button
                              className="icon-btn"
                              type="button"
                              title="删除文档"
                              data-testid="doc-delete"
                              onClick={() => setDocToDelete(document)}
                            >
                              <Icon name="trash" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {docsQuery.hasNextPage ? (
                <button
                  className="btn ghost sm"
                  type="button"
                  onClick={() => void docsQuery.fetchNextPage()}
                  disabled={docsQuery.isFetchingNextPage}
                >
                  {docsQuery.isFetchingNextPage ? "加载中…" : "加载更多文档"}
                </button>
              ) : null}
            </div>

            <div className="grid-2">
              <div className="panel">
                <div className="panel-head">
                  <div>
                    <h2>检索调试</h2>
                    <p>看召回与重排的分差，而不是只看最终答案。</p>
                  </div>
                </div>
                <div className="search-panel">
                  <div className="search-row">
                    <input
                      data-testid="kb-search-input"
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") submitSearch();
                      }}
                      placeholder="输入一个问题，例如「退款多久到账」"
                    />
                    <button
                      className="btn primary sm"
                      type="button"
                      data-testid="kb-search-submit"
                      onClick={submitSearch}
                      disabled={search.isPending || documents.length === 0}
                    >
                      <Icon name="search" />
                      检索
                    </button>
                  </div>

                  {search.isPending ? <div className="search-meta">检索中…</div> : null}
                  {search.isError ? (
                    <div className="search-meta">检索失败：{errorText(search.error)}</div>
                  ) : null}
                  {search.data ? (
                    <div className="search-panel" data-testid="search-results">
                      <div className="search-meta">
                        查询 <b>{search.data.query}</b> · 召回 <b>{search.data.recalled}</b> 条 ·
                        返回 <b>{search.data.returned}</b> 条 ·{" "}
                        {search.data.rerank_used ? "rerank" : "未 rerank"} ·{" "}
                        <b>{search.data.elapsed_ms}ms</b>
                      </div>
                      {search.data.items.length === 0 ? (
                        <div className="table-empty">没有命中；确认文档已入库且问题与内容相关。</div>
                      ) : null}
                      {search.data.items.map((item) => (
                        <div className="search-hit" data-testid="search-hit" key={item.chunk_id}>
                          <div className="hit-head">
                            <b>
                              {item.doc_name || "未命名文档"}
                              {item.page ? ` · 第 ${item.page} 页` : ""}
                              {item.heading_path ? ` · ${item.heading_path}` : ""}
                            </b>
                            <span className="scores">{scoreText(item)}</span>
                          </div>
                          <div className="hit-content">{item.content}</div>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              </div>

              <div className="panel">
                <div className="panel-head">
                  <div>
                    <h2>入库任务</h2>
                    <p>任务与在线上传解耦，进度独立可见。</p>
                  </div>
                  <button
                    className="btn ghost sm"
                    type="button"
                    onClick={() => void navigate({ to: "/tasks" })}
                  >
                    全部任务
                  </button>
                </div>
                <div className="search-panel">
                  {tasks.length === 0 ? <div className="table-empty">还没有任务。</div> : null}
                  {tasks.map((task) => (
                    <div key={task.id}>
                      <div className="task-line">
                        <span>{taskTypeLabel(task.type)}</span>
                        <span className={`chip ${statusTone(task.status)}`}>
                          {statusLabel(task.status)}
                        </span>
                      </div>
                      <div className="task-progress">
                        <div className="track">
                          <i style={{ width: `${task.progress}%` }} />
                        </div>
                        <span>{task.progress}%</span>
                      </div>
                      <div className="search-meta">
                        阶段 {task.stage || "—"} · 分片 {task.chunks_done}/{task.chunks_total}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </>
        ) : null}


      </div>

      {createOpen ? (
        <Modal
          title="新建知识库"
          subtitle="同名会冲突；切分参数只影响后续入库的文档。"
          onClose={() => setCreateOpen(false)}
          footer={
            <>
              <button className="btn ghost sm" type="button" onClick={() => setCreateOpen(false)}>
                取消
              </button>
              <button
                className="btn primary sm"
                type="button"
                data-testid="kb-submit"
                onClick={submitCreate}
                disabled={createKb.isPending}
              >
                创建
              </button>
            </>
          }
        >
          <div className="form-grid">
            <label className="field">
              <span>名称</span>
              <input
                data-testid="kb-name"
                value={name}
                maxLength={64}
                onChange={(event) => setName(event.target.value)}
                autoFocus
              />
            </label>
            <label className="field">
              <span>描述</span>
              <input
                data-testid="kb-desc"
                value={description}
                maxLength={500}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>
            <label className="field">
              <span>切片长度</span>
              <input
                data-testid="kb-chunk-size"
                type="number"
                min={128}
                max={2048}
                value={chunkSize}
                onChange={(event) => setChunkSize(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>重叠长度</span>
              <input
                data-testid="kb-chunk-overlap"
                type="number"
                min={0}
                value={chunkOverlap}
                onChange={(event) => setChunkOverlap(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>召回数</span>
              <input
                data-testid="kb-topk"
                type="number"
                min={1}
                max={100}
                value={topK}
                onChange={(event) => setTopK(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>重排保留</span>
              <input
                data-testid="kb-rerank"
                type="number"
                min={1}
                max={20}
                value={rerankTopN}
                onChange={(event) => setRerankTopN(Number(event.target.value))}
              />
            </label>
          </div>
        </Modal>
      ) : null}

      {deleteKbOpen && activeKb ? (
        <Modal
          title="删除知识库？"
          subtitle="会同时清理文档、向量与对象；这个操作不可撤销。"
          onClose={() => setDeleteKbOpen(false)}
          footer={
            <>
              <button className="btn ghost sm" type="button" onClick={() => setDeleteKbOpen(false)}>
                取消
              </button>
              <button
                className="btn danger sm"
                type="button"
                data-testid="delete-confirm"
                onClick={() => {
                  deleteKb.mutate(activeKb.id, {
                    onSuccess: () => {
                      setDeleteKbOpen(false);
                      setActiveKbId(null);
                      toast("知识库已删除", "ok");
                    },
                    onError: (error) => toast(errorText(error), "bad"),
                  });
                }}
                disabled={deleteKb.isPending}
              >
                确认删除
              </button>
            </>
          }
        >
          <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.7 }}>
            即将删除「{activeKb.name}」及其 {activeKb.document_count} 份文档、
            {activeKb.chunk_count} 个切片。
          </p>
        </Modal>
      ) : null}

      {docToDelete ? (
        <Modal
          title="删除文档？"
          subtitle="删除是异步任务；切片与向量会一并清理。"
          onClose={() => setDocToDelete(null)}
          footer={
            <>
              <button className="btn ghost sm" type="button" onClick={() => setDocToDelete(null)}>
                取消
              </button>
              <button
                className="btn danger sm"
                type="button"
                data-testid="delete-confirm"
                onClick={() => {
                  deleteDoc.mutate(docToDelete.id, {
                    onSuccess: () => {
                      setDocToDelete(null);
                      toast("已提交删除任务", "ok");
                    },
                    onError: (error) => toast(errorText(error), "bad"),
                  });
                }}
                disabled={deleteDoc.isPending}
              >
                确认删除
              </button>
            </>
          }
        >
          <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.7 }}>
            即将删除「{docToDelete.doc_name}」。若只是重新上传，建议先确认新文件已入库。
          </p>
        </Modal>
      ) : null}

      {chunkDoc ? (
        <Modal
          title={`切片 · ${chunkDoc.doc_name}`}
          subtitle={`${chunkDoc.chunk_count} 个切片 · 点击「加载更多」查看后续`}
          onClose={() => setChunkDoc(null)}
          footer={
            <button className="btn ghost sm" type="button" onClick={() => setChunkDoc(null)}>
              关闭
            </button>
          }
        >
          <div className="chunk-list">
            {chunksQuery.isPending ? <div className="table-empty">正在加载切片…</div> : null}
            {chunksQuery.isError ? (
              <div className="table-empty">切片加载失败：{errorText(chunksQuery.error)}</div>
            ) : null}
            {!chunksQuery.isPending && !chunksQuery.isError && chunks.length === 0 ? (
              <div className="table-empty">没有切片；文档可能尚未入库。</div>
            ) : null}
            {chunks.map((chunk) => (
              <div className="chunk-card" data-testid="chunk-item" key={chunk.chunk_id}>
                <div className="chunk-head">
                  <span className="mono">#{chunk.chunk_index}</span>
                  <span>{chunk.token_count} tokens</span>
                  {chunk.heading_path ? <span>{chunk.heading_path}</span> : null}
                  {chunk.page ? <span>第 {chunk.page} 页</span> : null}
                </div>
                <pre>{chunk.content}</pre>
              </div>
            ))}
            {chunksQuery.hasNextPage ? (
              <button
                className="btn ghost sm"
                type="button"
                onClick={() => void chunksQuery.fetchNextPage()}
                disabled={chunksQuery.isFetchingNextPage}
              >
                {chunksQuery.isFetchingNextPage ? "加载中…" : "加载更多"}
              </button>
            ) : null}
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
