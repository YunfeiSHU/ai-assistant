import { useAuthStore } from "@/features/auth/store";
import { Icon } from "@/shared/ui/Icon";

export function ChatPage() {
  const user = useAuthStore((state) => state.user);

  return (
    <section className="chat-view">
      <div className="chat-scroll">
        <div className="chat-inner">
          <div className="chat-empty">
            <div className="glyph">
              <Icon name="spark" size={22} />
            </div>
            <h2>你好，{user?.nickname || "朋友"}</h2>
            <p>
              从一个问题开始，或先选择左侧已有会话。需要资料时，在输入栏打开「资料」。
            </p>
            <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
              <span className="chip">流式回答</span>
              <span className="chip">知识库</span>
              <span className="chip">记忆与配额</span>
            </div>
          </div>
        </div>
      </div>

      <div className="composer">
        <div className="composer-box">
          <textarea
            rows={1}
            disabled
            placeholder="从左侧选择会话，或新建对话后开始输入…"
            aria-label="对话输入"
          />
          <div className="composer-toolbar">
            <span className="chip">选择会话后启用</span>
            <span className="quota-hint" style={{ marginLeft: "auto" }}>
              Enter 发送 · Shift+Enter 换行
            </span>
          </div>
        </div>
      </div>
    </section>
  );
}
