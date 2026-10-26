import type { MessageReference } from "@/features/messages/types";
import {
  downloadDocumentBlob,
  getDocumentDownloadInfo,
} from "@/features/knowledge/api";
import { errorText } from "@/shared/api/error";
import { toast } from "@/shared/ui/toast";

async function openOriginal(docId: string): Promise<void> {
  // 同步开窗，避免 await 之后被浏览器拦截弹窗。
  const popup = window.open("about:blank", "_blank");
  if (popup) popup.opener = null;
  try {
    const info = await getDocumentDownloadInfo(docId);
    if (info.url) {
      if (popup && !popup.closed) popup.location.href = info.url;
      else window.open(info.url, "_blank", "noopener,noreferrer");
      return;
    }
    const blob = await downloadDocumentBlob(docId);
    const objectUrl = URL.createObjectURL(blob);
    if (popup && !popup.closed) {
      popup.location.href = objectUrl;
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
    } else {
      window.open(objectUrl, "_blank", "noopener,noreferrer");
    }
  } catch (error) {
    popup?.close();
    toast(errorText(error), "bad");
  }
}

export function ReferenceDetails({
  messageId,
  references,
}: {
  messageId: string;
  references: MessageReference[];
}) {
  if (!references.length) return null;

  return (
    <details className="refs-block" id={`refs-${messageId}`} data-testid="references">
      <summary>
        引用 {references.length} 条资料
        <span className="refs-hint">点击角标可定位</span>
      </summary>
      <div className="ref-list">
        {references.map((reference) => (
          <div
            className="source-card"
            id={`ref-${messageId}-${reference.index}`}
            key={`${reference.chunk_id}-${reference.index}`}
          >
            <div className="source-head">
              <span className="source-index">{reference.index}</span>
              <div style={{ minWidth: 0 }}>
                <div className="source-doc">{reference.doc_name || "未命名文档"}</div>
                <div className="source-meta">
                  {[reference.heading_path, reference.page ? `第 ${reference.page} 页` : null]
                    .filter(Boolean)
                    .join(" · ") || "—"}
                </div>
              </div>
              <span className="source-score">{reference.score.toFixed(2)}</span>
            </div>
            <div className="source-snippet">{reference.snippet || "（无预览）"}</div>
            <div className="source-foot">
              <button
                className="btn ghost sm"
                type="button"
                data-testid="doc-original"
                onClick={(event) => {
                  event.preventDefault();
                  void openOriginal(reference.doc_id);
                }}
              >
                查看原文
              </button>
            </div>
            <div className="score-bar">
              <i style={{ width: `${Math.round(reference.score * 100)}%` }} />
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}
