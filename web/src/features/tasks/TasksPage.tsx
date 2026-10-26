import { useEffect, useMemo, useState } from "react";

import { useQueryClient } from "@tanstack/react-query";

import { isApiError } from "@/shared/api/error";
import { formatDateTime } from "@/shared/lib/format";
import { Icon } from "@/shared/ui/Icon";
import { toast } from "@/shared/ui/toast";

import { streamTaskEvents } from "./api";
import { isActiveTask, taskKeys, useCancelTask, useRetryTask, useTasks } from "./queries";
import type { Task, TaskStatus } from "./types";

type FilterKey = "all" | TaskStatus;

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "RUNNING", label: "运行中" },
  { key: "QUEUED", label: "排队" },
  { key: "FAILED", label: "失败" },
  { key: "SUCCEEDED", label: "已完成" },
  { key: "CANCELED", label: "已取消" },
];

function errorText(error: unknown): string {
  if (isApiError(error)) return `${error.code} · ${error.message}`;
  return "请求失败，请稍后重试";
}

function statusTone(status: string): string {
  if (status === "SUCCEEDED") return "ok";
  if (status === "FAILED" || status === "CANCELED") return "bad";
  if (["PENDING", "QUEUED", "RUNNING"].includes(status)) return "warn";
  return "";
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    PENDING: "待处理",
    QUEUED: "排队",
    RUNNING: "运行中",
    SUCCEEDED: "已完成",
    FAILED: "失败",
    CANCELED: "已取消",
  };
  return labels[status] ?? status;
}

function typeLabel(type: string): string {
  const labels: Record<string, string> = {
    document_ingest: "文档入库",
    document_delete: "文档删除",
    kb_reindex: "知识库重建",
    summary_build: "摘要生成",
    memory_extract: "记忆抽取",
  };
  return labels[type] ?? type;
}

function flatten(pages: { items: Task[] }[] | undefined): Task[] {
  const seen = new Set<string>();
  const items: Task[] = [];
  for (const page of pages ?? []) {
    for (const task of page.items) {
      if (seen.has(task.id)) continue;
      seen.add(task.id);
      items.push(task);
    }
  }
  return items;
}

export function TasksPage() {
  const [filter, setFilter] = useState<FilterKey>("all");
  const tasksQuery = useTasks({
    status: filter === "all" ? undefined : filter,
    limit: 30,
  });
  const tasks = useMemo(() => flatten(tasksQuery.data?.pages), [tasksQuery.data]);

  const cancel = useCancelTask();
  const retry = useRetryTask();
  const queryClient = useQueryClient();
  const activeTaskId = tasks.find(isActiveTask)?.id;

  useEffect(() => {
    if (!activeTaskId) return;
    const controller = new AbortController();
    void streamTaskEvents(
      activeTaskId,
      () => {
        void queryClient.invalidateQueries({ queryKey: taskKeys.all });
      },
      controller.signal,
    ).catch(() => {
      // SSE 不可用时保留 2s 轮询兜底，不向用户报错。
    });
    return () => controller.abort();
  }, [activeTaskId, queryClient]);

  const running = tasks.filter((task) => task.status === "RUNNING").length;
  const queued = tasks.filter((task) => task.status === "QUEUED").length;
  const failed = tasks.filter((task) => task.status === "FAILED").length;

  function handleCancel(task: Task) {
    cancel.mutate(task.id, {
      onSuccess: () => toast("已请求取消；运行中的批次会在检查点退出", "warn"),
      onError: (error) => toast(errorText(error), "bad"),
    });
  }

  function handleRetry(task: Task) {
    retry.mutate(task.id, {
      onSuccess: () => toast("已重新入队", "ok"),
      onError: (error) => toast(errorText(error), "bad"),
    });
  }

  return (
    <section className="page">
      <div className="page-inner">
        <div className="panel">
          <div className="panel-head">
            <div>
              <h2>任务中心</h2>
              <p>状态机：PENDING → QUEUED → RUNNING → SUCCEEDED / FAILED / CANCELED。</p>
            </div>
            <div className="right">
              <span className="search-meta">
                最近刷新{" "}
                {tasksQuery.dataUpdatedAt
                  ? formatDateTime(new Date(tasksQuery.dataUpdatedAt).toISOString())
                  : "—"}
              </span>
              <button
                className="btn ghost sm"
                type="button"
                data-testid="task-refresh"
                onClick={() => void tasksQuery.refetch()}
                disabled={tasksQuery.isFetching}
              >
                <Icon name="refresh" />
                {tasksQuery.isFetching ? "刷新中…" : "刷新"}
              </button>
            </div>
          </div>

          <div className="filter-bar">
            {FILTERS.map((item) => (
              <button
                className={`filter${filter === item.key ? " active" : ""}`}
                type="button"
                key={item.key}
                data-testid="task-filter"
                onClick={() => setFilter(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>

          {tasksQuery.isError ? (
            <div className="table-empty">
              任务加载失败：{errorText(tasksQuery.error)}{" "}
              <button className="btn ghost sm" type="button" onClick={() => void tasksQuery.refetch()}>
                重试
              </button>
            </div>
          ) : null}

          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>类型</th>
                  <th>资源</th>
                  <th>状态</th>
                  <th>进度</th>
                  <th>分片</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {tasksQuery.isPending ? (
                  <tr>
                    <td colSpan={6}>
                      <div className="table-empty">正在加载任务…</div>
                    </td>
                  </tr>
                ) : null}
                {!tasksQuery.isPending && !tasksQuery.isError && tasks.length === 0 ? (
                  <tr>
                    <td colSpan={6}>
                      <div className="table-empty">没有该状态的任务。</div>
                    </td>
                  </tr>
                ) : null}
                {tasks.map((task) => (
                  <tr key={task.id} data-testid="task-row">
                    <td className="mono">{typeLabel(task.type)}</td>
                    <td className="name">
                      <span className="mono" title={task.resource_id}>
                        {task.resource_id.length > 16
                          ? `${task.resource_id.slice(0, 14)}…`
                          : task.resource_id}
                      </span>
                    </td>
                    <td>
                      <span className={`chip ${statusTone(task.status)}`} data-testid="task-status">
                        {task.cancel_requested ? "取消中…" : statusLabel(task.status)}
                      </span>
                      {task.error ? (
                        <div className="doc-error" title={task.error.message}>
                          {task.error.code}
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <div className="task-progress">
                        <div className="track">
                          <i style={{ width: `${task.progress}%` }} />
                        </div>
                        <span>{task.progress}%</span>
                      </div>
                      <div className="search-meta">
                        {task.stage || "—"} · 重试 {task.retry_count}/{task.max_retries}
                      </div>
                    </td>
                    <td>
                      {task.chunks_total ? `${task.chunks_done}/${task.chunks_total}` : "—"}
                    </td>
                    <td>
                      <div className="actions">
                        {task.cancelable ? (
                          <button
                            className="btn ghost sm"
                            type="button"
                            data-testid="task-cancel"
                            onClick={() => handleCancel(task)}
                            disabled={cancel.isPending || task.cancel_requested}
                            title={task.cancel_requested ? "已请求取消，等待检查点退出" : undefined}
                          >
                            {task.cancel_requested ? "取消中…" : "取消"}
                          </button>
                        ) : null}
                        {task.retryable ? (
                          <button
                            className="btn ghost sm"
                            type="button"
                            data-testid="task-retry"
                            onClick={() => handleRetry(task)}
                            disabled={retry.isPending}
                          >
                            重试
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {tasksQuery.hasNextPage ? (
            <button
              className="btn ghost sm"
              type="button"
              onClick={() => void tasksQuery.fetchNextPage()}
              disabled={tasksQuery.isFetchingNextPage}
            >
              {tasksQuery.isFetchingNextPage ? "加载中…" : "加载更多任务"}
            </button>
          ) : null}
        </div>

        <div className="grid-3">
          <div className="panel">
            <div className="search-meta">运行中</div>
            <div className="stat-number">{running}</div>
          </div>
          <div className="panel">
            <div className="search-meta">排队</div>
            <div className="stat-number">{queued}</div>
          </div>
          <div className="panel">
            <div className="search-meta">失败待重试</div>
            <div className="stat-number">{failed}</div>
          </div>
        </div>

        <div className="search-meta" style={{ textAlign: "right" }}>
          有进行中的任务时每 2s 自动刷新；全部终态后停止轮询。
        </div>
      </div>
    </section>
  );
}
