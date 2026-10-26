import { errorAction } from "@/shared/api/error-actions";

import type { StreamErrorInfo } from "./stream/types";

export function StreamErrorCard({ error }: { error: StreamErrorInfo }) {
  const action = errorAction(error.code);
  return (
    <div className="stream-error" data-testid="stream-error">
      <div className="stream-error-head">
        <strong>{error.code}</strong>
        <span className={`chip${error.retryable ? " warn" : " bad"}`}>
          {error.retryable ? "可重试" : "不可重试"}
        </span>
        <span className="chip">建议：{action.label}</span>
      </div>
      <p>{error.message}</p>
      {error.traceId ? (
        <code title="trace_id" className="stream-trace">
          {error.traceId}
        </code>
      ) : null}
    </div>
  );
}
