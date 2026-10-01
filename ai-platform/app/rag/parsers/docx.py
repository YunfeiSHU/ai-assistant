"""DOCX 解析（``python-docx``）。

``docs/06`` §4.1 要求 MUST NOT 丢失表格内容：企业文档里的规格、报价、对照关系常常只存在
于表格里，丢掉表格等于丢掉最需要被检索的那部分。

按文档流顺序遍历 ``document.element.body``，而不是分别遍历 ``paragraphs`` 与 ``tables``
—— 后者会把表格全部搬到末尾，正文与表格的相对位置就乱了，切出的上下文会缺失指代对象。
"""

from __future__ import annotations

import io

from docx import Document as DocxDocument
from docx.document import Document as DocxDocumentType
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.exceptions import AppError, ErrorCode
from app.core.text import normalize_text
from app.rag.parsers.base import BlockBuilder, ParsedDocument

_TABLE_CELL_SEPARATOR = " | "
#: 段落样式名 → 标题层级（中英文 Word 都覆盖）
_HEADING_STYLES = {
    "heading 1": 1,
    "heading 2": 2,
    "heading 3": 3,
    "heading 4": 4,
    "heading 5": 5,
    "heading 6": 6,
    "标题 1": 1,
    "标题 2": 2,
    "标题 3": 3,
    "标题 4": 4,
    "标题 5": 5,
    "标题 6": 6,
}


class DocxParser:
    """``.docx`` 解析器。"""

    extensions = (".docx",)

    def parse(self, raw: bytes, *, filename: str) -> ParsedDocument:
        try:
            document = DocxDocument(io.BytesIO(raw))
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.UNPROCESSABLE_DOCUMENT,
                f"DOCX 解析失败：{exc}",
                {"filename": filename},
            ) from exc

        builder = BlockBuilder()
        stack: list[tuple[int, str]] = []
        paragraph_buffer: list[str] = []

        def path() -> str:
            return " > ".join(title for _, title in stack)

        def flush() -> None:
            if paragraph_buffer:
                builder.add("\n".join(paragraph_buffer), heading_path=path())
                paragraph_buffer.clear()

        for block in _iter_body(document):
            if isinstance(block, Paragraph):
                text = normalize_text(block.text)
                if not text:
                    continue
                level = _heading_level(block)
                if level is not None:
                    flush()
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    stack.append((level, text))
                    continue
                paragraph_buffer.append(text)
            else:
                flush()
                rendered = _render_table(block)
                if rendered:
                    builder.add(rendered, heading_path=path(), atomic=True)
        flush()

        return builder.build(
            table_count=sum(1 for b in _iter_body(document) if isinstance(b, Table))
        )


def _iter_body(document: DocxDocumentType) -> list[Paragraph | Table]:
    """按文档流顺序取出段落与表格。"""
    result: list[Paragraph | Table] = []
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            result.append(Paragraph(child, document))
        elif tag == "tbl":
            result.append(Table(child, document))
    return result


def _heading_level(paragraph: Paragraph) -> int | None:
    style = paragraph.style
    name = (style.name or "").strip().lower() if style is not None else ""
    if name in _HEADING_STYLES:
        return _HEADING_STYLES[name]
    # 有些文档用 ``Heading1`` / ``标题1`` 这种无空格写法
    normalized = name.replace(" ", "")
    for candidate, level in (
        ("heading1", 1),
        ("heading2", 2),
        ("heading3", 3),
        ("标题1", 1),
        ("标题2", 2),
        ("标题3", 3),
    ):
        if normalized == candidate:
            return level
    return None


def _render_table(table: Table) -> str:
    """把表格渲染成带 ``|`` 分隔的文本行。

    刻意保留成文本而不是结构化存储：检索与 Embedding 都只吃文本。单元格内换行折叠成
    空格，避免一行表格在渲染后变成多行、破坏「一行一记录」的可读性。
    """
    rows: list[str] = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()) for cell in row.cells]
        if any(cells):
            rows.append(_TABLE_CELL_SEPARATOR.join(cells))
    return "\n".join(rows)


__all__ = ["DocxParser"]
