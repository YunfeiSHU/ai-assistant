"""文档解析器端口（``docs/06`` §4.1）。

统一产出 :class:`ParsedDocument`：正文按「块」组织，每块自带 ``page`` /
``heading_path`` / ``atomic`` 三个定位信息。切分器只认这三个字段，因此新增格式
（如 PPTX）只需要写一个解析器，不必碰切分与检索。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.rag.chunking import SourceBlock


@dataclass(slots=True)
class ParsedDocument:
    """解析结果。"""

    #: 归一化后的正文块（供切分）
    blocks: list[SourceBlock] = field(default_factory=list)
    #: 页数（PDF / DOCX 计实际页数；其余格式为 1）
    page_count: int = 1
    #: 正文总字符数（清洗后）
    char_count: int = 0
    #: 原始正文拼接（``char_start``/``char_end`` 的坐标基准）
    text: str = ""
    #: 解析器补充的元信息（如 ``{"encoding": "gb18030"}``）
    metadata: dict[str, object] = field(default_factory=dict)

    def is_empty(self, *, min_chars: int) -> bool:
        """有效正文是否不足 ``min_chars``。

        典型触发场景是扫描版 PDF：解析成功但一个字都没有。这种情况必须报
        ``422 UNPROCESSABLE_DOCUMENT`` 而不是「入库成功但检索不到东西」。
        """
        return self.char_count < min_chars


@runtime_checkable
class DocumentParser(Protocol):
    """格式解析器。"""

    @property
    def extensions(self) -> tuple[str, ...]:
        """支持的扩展名（小写，含点）。"""
        ...

    def parse(self, raw: bytes, *, filename: str) -> ParsedDocument:
        """解析文件内容。

        Raises:
            AppError: ``422 UNPROCESSABLE_DOCUMENT``（格式对但内容不可用）。
        """
        ...


def build_blocks_from_pages(
    pages: Sequence[str],
    *,
    heading_paths: Sequence[str] | None = None,
) -> tuple[list[SourceBlock], str, int]:
    """把「按页文本」拼成块列表，同时给出整篇正文与总字符数。

    ``offset`` 以**拼接后**的正文为基准，而拼接规则（页间 ``\\n\\n``）在这里
    唯一定义。让各解析器自己算偏移量是这类代码最经典的错位来源：
    引用溯源报出的字符区间会在第 2 页之后全部漂移。
    """
    blocks: list[SourceBlock] = []
    parts: list[str] = []
    offset = 0
    for index, page in enumerate(pages):
        if index > 0:
            parts.append("\n\n")
            offset += 2
        text = page.strip()
        heading = heading_paths[index] if heading_paths and index < len(heading_paths) else ""
        if text:
            blocks.append(
                SourceBlock(text=text, offset=offset, page=index + 1, heading_path=heading)
            )
        parts.append(page)
        offset += len(page)
    joined = "".join(parts)
    return blocks, joined, len(joined)


class BlockBuilder:
    """逐块累积正文，同时维护每块在整篇正文里的字符偏移。

    Markdown / HTML / DOCX 都是「流式遇到结构边界就切块」，逐块累加偏移比事后
    用 ``str.find`` 反推可靠：重复段落会让 ``find`` 定位到第一处，字符区间就错了。
    """

    SEPARATOR = "\n\n"

    def __init__(self) -> None:
        self.blocks: list[SourceBlock] = []
        self._parts: list[str] = []
        self._offset = 0

    def add(
        self,
        text: str,
        *,
        page: int | None = None,
        heading_path: str = "",
        atomic: bool = False,
    ) -> None:
        """追加一块；空白块直接丢弃。"""
        content = text.strip()
        if not content:
            return
        if self._parts:
            self._parts.append(self.SEPARATOR)
            self._offset += len(self.SEPARATOR)
        self.blocks.append(
            SourceBlock(
                text=content,
                offset=self._offset,
                page=page,
                heading_path=heading_path,
                atomic=atomic,
            )
        )
        self._parts.append(content)
        self._offset += len(content)

    @property
    def text(self) -> str:
        """整篇正文（``char_start``/``char_end`` 的坐标基准）。"""
        return "".join(self._parts)

    @property
    def char_count(self) -> int:
        return len(self.text)

    def build(self, *, page_count: int = 1, **metadata: object) -> ParsedDocument:
        """产出 :class:`ParsedDocument`。"""
        text = self.text
        return ParsedDocument(
            blocks=list(self.blocks),
            page_count=page_count,
            char_count=len(text),
            text=text,
            metadata=dict(metadata),
        )


__all__ = [
    "BlockBuilder",
    "DocumentParser",
    "ParsedDocument",
    "build_blocks_from_pages",
]
