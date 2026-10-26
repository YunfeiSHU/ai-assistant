import type { TurnState } from "./stream/types";
import { ReferenceDetails } from "./ReferenceDetails";
import { ToolTrace } from "./ToolTrace";
import { Markdown } from "@/shared/ui/Markdown";

function phaseLabel(turn: TurnState): string {
  if (turn.phase === "connecting") return "正在连接…";
  if (turn.phase === "streaming") return "正在生成…";
  if (turn.phase === "aborted") return "已停止";
  if (turn.phase === "error") return "生成失败";
  return "完成";
}

export function LiveTurn({
  turn,
  onCitation,
}: {
  turn: TurnState;
  onCitation: (messageId: string, index: number) => void;
}) {
  if (turn.phase === "idle") return null;

  const messageId = turn.assistantId ?? "live";
  const tokens = turn.usage?.total_tokens;
  const elapsed = turn.elapsedMs;
  const done = turn.phase === "done" || turn.phase === "aborted" || turn.phase === "error";

  return (
    <>
      {turn.userText ? (
        <article
          className="msg user"
          data-testid="live-user"
          data-message-id={turn.userMessageId ?? undefined}
        >
          <div>
            <div className="bubble">{turn.userText}</div>
          </div>
        </article>
      ) : null}

      <article className="msg assistant" data-testid="live-turn">
        <div className="msg-avatar">AI</div>
        <div className="msg-body">
          <div className="msg-top">
            <span className="model-badge">{turn.model || "…"}</span>
            <span className="msg-time">{phaseLabel(turn)}</span>
            {turn.partial || turn.phase === "aborted" ? (
              <span className="chip warn" data-testid="partial-chip">
                partial
              </span>
            ) : null}
            {turn.degraded ? <span className="chip warn">degraded</span> : null}
          </div>

          <ToolTrace tools={turn.tools} />

          {turn.content ? (
            <div className="live-md">
              <Markdown
                content={turn.content}
                onCitation={(index) => onCitation(messageId, index)}
              />
              {!done ? <span className="cursor" /> : null}
            </div>
          ) : (
            <div className="live-thinking">
              <span className="thinking">
                <i />
                <i />
                <i />
              </span>
              <span>{phaseLabel(turn)}</span>
            </div>
          )}

          <ReferenceDetails messageId={messageId} references={turn.references} />

          <div className="msg-meta">
            {tokens != null ? <span>{tokens} tokens</span> : null}
            {elapsed != null ? (
              <>
                <span className="sep">·</span>
                <span>{(elapsed / 1000).toFixed(1)}s</span>
              </>
            ) : null}
            {turn.finishReason ? (
              <>
                <span className="sep">·</span>
                <span>{turn.finishReason}</span>
              </>
            ) : null}
            {turn.persistError ? (
              <span className="chip warn" title={turn.persistError}>
                保存失败
              </span>
            ) : null}
          </div>
        </div>
      </article>
    </>
  );
}
