import { useEffect, useState } from "react";

import { isApiError } from "@/shared/api/error";
import { formatBytes, formatDateTime, formatNumber } from "@/shared/lib/format";
import { Icon } from "@/shared/ui/Icon";

import { useQuota, useUsage } from "./queries";
import type { QuotaMetric } from "./types";

function errorText(error: unknown): string {
  if (isApiError(error)) return `${error.code} · ${error.message}`;
  return "请求失败，请稍后重试";
}

const METRIC_LABEL: Record<string, string> = {
  chat_requests: "对话请求",
  llm_tokens: "LLM tokens",
  embedding_tokens: "向量化 tokens",
  kb_count: "知识库",
  document_count: "文档数",
  documents_count: "文档数",
  storage_bytes: "存储",
  upload_bytes: "上传流量",
  concurrency: "并发",
};

function metricLabel(metric: string): string {
  return METRIC_LABEL[metric] ?? metric;
}

function formatAmount(metric: string, amount: number): string {
  if (amount === 0) return metric.includes("bytes") ? "0 B" : "0";
  if (metric.includes("bytes")) return formatBytes(amount);
  return formatNumber(amount);
}

function remainingText(metric: QuotaMetric): string {
  if (metric.limit == null) return "不限量";
  if (metric.remaining <= 0) return "已用尽";
  return `${formatAmount(metric.metric, metric.remaining)} 剩余`;
}

export function QuotaPage() {
  const quotaQuery = useQuota();
  const quota = quotaQuery.data;

  const defaultTo = new Date().toISOString();
  const defaultFrom = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const usageQuery = useUsage(quota?.period.start ?? defaultFrom, quota?.period.end ?? defaultTo);

  const metrics = quota?.metrics ?? [];
  const exhausted = metrics.filter((metric) => metric.limit != null && metric.remaining <= 0);

  return (
    <section className="page">
      <div className="page-inner">
        <div className="panel">
          <div className="panel-head">
            <div>
              <h2>配额与用量</h2>
              <p>
                {quota ? `${quota.plan} 套餐 · ${formatDateTime(quota.period.start)} → ${formatDateTime(quota.period.end)}` : "正在加载配额…"}
              </p>
            </div>
            <button
              className="btn ghost sm"
              type="button"
              data-testid="quota-refresh"
              onClick={() => {
                void quotaQuery.refetch();
                void usageQuery.refetch();
              }}
              disabled={quotaQuery.isFetching}
            >
              <Icon name="refresh" />
              {quotaQuery.isFetching ? "刷新中…" : "刷新"}
            </button>
          </div>

          {quotaQuery.isError ? (
            <div className="table-empty">
              配额加载失败：{errorText(quotaQuery.error)}{" "}
              <button className="btn ghost sm" type="button" onClick={() => void quotaQuery.refetch()}>
                重试
              </button>
            </div>
          ) : null}

          {exhausted.length ? (
            <div className="stream-warning" data-testid="quota-exhausted">
              {exhausted.map((metric) => `${metricLabel(metric.metric)} 已用尽`).join("、")}
              ；请等到重置时间后再试，或升级套餐。
            </div>
          ) : null}

          <div className="quota-grid">
            {metrics.map((metric) => {
              const ratio =
                metric.limit == null || metric.limit <= 0
                  ? 0
                  : Math.min(100, Math.round((metric.used / metric.limit) * 100));
              return (
                <div className="panel quota-card" data-testid="quota-metric" key={metric.metric}>
                  <div className="quota-head">
                    <span>{metricLabel(metric.metric)}</span>
                    <span className={`chip${metric.limit != null && metric.remaining <= 0 ? " bad" : ""}`}>
                      {remainingText(metric)}
                    </span>
                  </div>
                  <div className="quota-value" data-testid="quota-value">
                    {formatAmount(metric.metric, metric.used)}
                    <span className="quota-limit">
                      {metric.limit == null ? " / 不限" : ` / ${formatAmount(metric.metric, metric.limit)}`}
                    </span>
                  </div>
                  <div className="task-progress">
                    <div className="track">
                      <i style={{ width: `${ratio}%` }} />
                    </div>
                    <span>{metric.limit == null ? "—" : `${ratio}%`}</span>
                  </div>
                  <div className="search-meta">
                    {metric.reset_at ? `重置 ${formatDateTime(metric.reset_at)}` : "不自动重置"}
                    {metric.limit != null && metric.remaining <= 0 && metric.reset_at
                      ? ` · ${Math.max(0, Math.ceil((new Date(metric.reset_at).getTime() - now) / 1000))}s`
                      : ""}
                  </div>
                </div>
              );
            })}
          </div>

          {!quotaQuery.isPending && metrics.length === 0 ? (
            <div className="table-empty">暂无配额指标。</div>
          ) : null}
        </div>

        <div className="panel">
          <div className="panel-head">
            <div>
              <h2>用量明细</h2>
              <p>
                本周期合计：对话 {formatNumber(usageQuery.data?.totals.chat_requests ?? 0)} 次 ·
                LLM {formatNumber(usageQuery.data?.totals.llm_tokens ?? 0)} tokens
              </p>
            </div>
            <span className="search-meta">
              {usageQuery.data
                ? `${formatDateTime(usageQuery.data.from)} → ${formatDateTime(usageQuery.data.to)}`
                : "最近 24 小时"}
            </span>
          </div>

          {usageQuery.isError ? (
            <div className="table-empty">
              用量加载失败：{errorText(usageQuery.error)}{" "}
              <button className="btn ghost sm" type="button" onClick={() => void usageQuery.refetch()}>
                重试
              </button>
            </div>
          ) : null}

          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>指标</th>
                  <th>数量</th>
                  <th>会话 / 消息</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {usageQuery.isPending ? (
                  <tr>
                    <td colSpan={4}>
                      <div className="table-empty">正在加载用量…</div>
                    </td>
                  </tr>
                ) : null}
                {!usageQuery.isPending && !usageQuery.isError && (usageQuery.data?.items.length ?? 0) === 0 ? (
                  <tr>
                    <td colSpan={4}>
                      <div className="table-empty">这个周期还没有用量。</div>
                    </td>
                  </tr>
                ) : null}
                {usageQuery.data?.items.map((item) => (
                  <tr data-testid="usage-row" key={item.id}>
                    <td className="mono">{metricLabel(item.metric)}</td>
                    <td>{formatAmount(item.metric, item.amount)}</td>
                    <td className="mono" title={item.conversation_id ?? ""}>
                      {item.conversation_id ? `${item.conversation_id.slice(0, 12)}…` : "—"}
                      {item.message_id ? ` / ${item.message_id.slice(0, 10)}…` : ""}
                    </td>
                    <td>{formatDateTime(item.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  );
}
