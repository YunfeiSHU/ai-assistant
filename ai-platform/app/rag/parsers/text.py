"""纯文本解析（编码探测 UTF-8 → UTF-8-BOM → GB18030）。

按空行分段成块：这让切分器有机会在段落边界断开，而不是拿到一整坨文本后
只能靠标点硬切。
"""

from __future__ import annotations

import re

from app.core.text import decode_bytes, normalize_text
from app.rag.parsers.base import BlockBuilder, ParsedDocument

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


class TextParser:
    """``.txt`` 解析器。"""

    extensions = (".txt",)

    def parse(self, raw: bytes, *, filename: str) -> ParsedDocument:
        text, encoding = decode_bytes(raw)
        builder = BlockBuilder()
        for paragraph in _PARAGRAPH_SPLIT.split(normalize_text(text)):
            builder.add(paragraph)
        return builder.build(encoding=encoding)


__all__ = ["TextParser"]
