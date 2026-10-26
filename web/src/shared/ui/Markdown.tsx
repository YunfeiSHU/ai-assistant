import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import type { ReactNode } from "react";
import "katex/dist/katex.min.css";

/**
 * 助手正文的 Markdown 渲染；模型输出一律当不可信内容，过 sanitize。
 *
 * 引用角标：先把 `[n]` 预处理成 `[n](#ref-n)` 链接，再在 `a` 渲染器里换成按钮。
 * 这样不用引入额外的 remark AST 插件，也能让角标可点击。
 */
export function Markdown({
  content,
  onCitation,
}: {
  content: string;
  onCitation?: (index: number) => void;
}) {
  const prepared = content.replace(/\[(\d+)\]/g, "[$1](#ref-$1)");

  const components: Components = {
    a: ({ href, children }) => {
      const match = href?.match(/^#ref-(\d+)$/);
      if (match) {
        const index = Number(match[1]);
        return (
          <button
            type="button"
            className="cite"
            onClick={() => onCitation?.(index)}
            title={`查看引用 ${index}`}
          >
            {index}
          </button>
        );
      }
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children as ReactNode}
        </a>
      );
    },
  };

  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeSanitize, rehypeKatex]}
        components={components}
      >
        {prepared}
      </ReactMarkdown>
    </div>
  );
}
