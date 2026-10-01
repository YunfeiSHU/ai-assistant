"""HTML 解析（``BeautifulSoup``）。

``docs/06`` §4.1：去掉 ``script`` / ``style`` / ``nav`` / ``footer``；按 ``<h1..h6>``
生成 ``heading_path``。

必须删 ``nav`` / ``footer``：它们几乎出现在每一页且高度重复，会被检索反复命中
（「上一页 / 下一页 / 版权所有」成为最高分片段），把真正的答案挤掉。
"""

from __future__ import annotations

from bs4 import BeautifulSoup, NavigableString, Tag

from app.core.exceptions import AppError, ErrorCode
from app.core.text import decode_bytes, normalize_text
from app.rag.parsers.base import BlockBuilder, ParsedDocument

#: 结构性噪声：一律丢弃（不参与正文）
_DROP_TAGS = ("script", "style", "nav", "footer", "noscript", "head", "iframe", "svg")
#: 块级标签：遇到就断开当前段落
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "li",
        "tr",
        "blockquote",
        "pre",
        "table",
        "ul",
        "ol",
        "br",
        "hr",
        "dt",
        "dd",
        "figcaption",
    }
)
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")


class HtmlParser:
    """``.html`` / ``.htm`` 解析器。"""

    extensions = (".html", ".htm")

    def parse(self, raw: bytes, *, filename: str) -> ParsedDocument:
        source, encoding = decode_bytes(raw)
        try:
            soup = BeautifulSoup(source, "html.parser")
        except Exception as exc:
            raise AppError(
                ErrorCode.UNPROCESSABLE_DOCUMENT,
                f"HTML 解析失败：{exc}",
                {"filename": filename},
            ) from exc

        # ``<title>`` 必须在 ``decompose`` 之前取：``head`` 也在丢弃列表里，
        # 先删再读的话 ``html_title`` 永远是空字符串。
        page_title = soup.title.get_text(strip=True) if soup.title else ""
        for tag in soup.find_all(_DROP_TAGS):
            tag.decompose()

        builder = BlockBuilder()
        stack: list[tuple[int, str]] = []

        def path() -> str:
            return " > ".join(title for _, title in stack)

        paragraph: list[str] = []

        def flush() -> None:
            text = normalize_text("".join(paragraph))
            if text:
                builder.add(text, heading_path=path())
            paragraph.clear()

        def walk(node: Tag) -> None:
            """按文档流遍历，收集文本。

            必须递归而不是 ``for element in soup.descendants``：后者迭代到 ``<p>`` 时
            只能决定「断开段落」，而真正的文字在 ``<p>`` 的文本子节点上，会被最外层那句
            ``if not isinstance(element, Tag)`` 整个跳过 —— 结果是所有块级标签里的正文
            全部丢失（真实网页几乎都把文字放在 ``<p>`` / ``<div>`` / ``<li>`` 里）。
            递归还要显式跳过 ``table`` / ``pre`` 的子树，否则表格内容会被既作为原子块、
            又作为普通文本重复收两次。
            """
            for child in node.children:
                if isinstance(child, NavigableString):
                    paragraph.append(str(child) + " ")
                    continue
                if not isinstance(child, Tag):
                    continue
                name = child.name.lower()
                if name in _HEADING_TAGS:
                    flush()
                    level = int(name[1])
                    title = " ".join(child.get_text(" ", strip=True).split())
                    if title:
                        while stack and stack[-1][0] >= level:
                            stack.pop()
                        stack.append((level, title))
                    continue
                if name in ("table", "pre"):
                    # 表格与代码块原子化；子树不再遍历，避免重复收取
                    flush()
                    builder.add(_render_verbatim(child), heading_path=path(), atomic=True)
                    continue
                if name in _BLOCK_TAGS:
                    # 块级边界：先收尾上一段，再进入子树收本段文字
                    flush()
                walk(child)

        walk(soup)
        flush()
        return builder.build(
            encoding=encoding,
            html_title=page_title,
            heading_count=len(stack),
        )


def _render_verbatim(element: Tag) -> str:
    """原样渲染表格 / 代码块，保留行结构。"""
    if element.name == "table":
        rows: list[str] = []
        for row in element.find_all("tr"):
            cells = [
                " ".join(cell.get_text(" ", strip=True).split())
                for cell in row.find_all(["td", "th"])
            ]
            if any(cells):
                rows.append(" | ".join(cells))
        return "\n".join(rows)
    return element.get_text("\n", strip=True)


__all__ = ["HtmlParser"]
