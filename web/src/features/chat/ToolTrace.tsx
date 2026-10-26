import type { ToolCallTrace } from "@/features/messages/types";
import { Icon } from "@/shared/ui/Icon";

function label(status: ToolCallTrace["status"]): string {
  if (status === "running") return "running";
  if (status === "ok") return "ok";
  if (status === "timeout") return "timeout";
  if (status === "forbidden") return "forbidden";
  return "error";
}

export function ToolTrace({ tools }: { tools: ToolCallTrace[] }) {
  if (!tools.length) return null;

  return (
    <div className="tool-slot">
      {tools.map((tool) => (
        <details
          className={`tool ${tool.status}`}
          key={`${tool.call_id}-${tool.name}`}
          open={tool.status === "running"}
          data-testid="tool-trace"
        >
          <summary>
            <span className="status-dot" />
            <span className="tool-name">{tool.name}</span>
            <span
              className={`chip${
                tool.status === "ok" ? " ok" : tool.status === "timeout" ? " warn" : ""
              }`}
            >
              {label(tool.status)}
            </span>
            <span className="tool-elapsed">
              {tool.status === "running" ? "running" : `${tool.elapsed_ms} ms`}
            </span>
            <span className="tool-chevron">
              <Icon name="chevron" />
            </span>
          </summary>
          <div className="tool-body">
            <b>arguments</b>
            {"\n"}
            {JSON.stringify(tool.arguments, null, 2)}
            {"\n\n"}
            <b>result</b>
            {"\n"}
            {tool.summary || (tool.status === "running" ? "执行中…" : "（无摘要）")}
          </div>
        </details>
      ))}
    </div>
  );
}
