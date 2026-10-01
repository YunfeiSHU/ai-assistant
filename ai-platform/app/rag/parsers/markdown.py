"""Markdown 解析（原生读取，保留标题层级）。

``docs/06`` §4.1 要求写入 ``heading_path``（如 ``售后政策 > 退款 > 时效``）：检索命中后它
决定引用卡片上给用户看的位置，也是判断「这段是否在讲同一个主题」的最廉价信号。

代码块与表格按 §4.3 作为原子块：把一张表在中间切开，两半都读不出含义。
"""

from __future__ import annotations

import re

from app.core.text import decode_bytes, normalize_text
from app.rag.parsers.base import BlockBuilder, ParsedDocument

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW = re.compile(r"^\s*\|")
_MIN_TABLE_ROWS = 2


class MarkdownParser:
    """``.md`` / ``.markdown`` 解析器。"""

    extensions = (".md", ".markdown")

    def parse(self, raw: bytes, *, filename: str) -> ParsedDocument:
        text, encoding = decode_bytes(raw)
        builder = BlockBuilder()
        stack: list[tuple[int, str]] = []
        paragraph: list[str] = []

        def path() -> str:
            return " > ".join(title for _, title in stack)

        def flush() -> None:
            if paragraph:
                builder.add("\n".join(paragraph), heading_path=path())
                paragraph.clear()

        # ``join_cjk_lines=False`` 是必须的，不是风格选择：Markdown 的行结构是语义
        # （``## 标题`` 独占一行）。默认归一会把「``## 年假`` + 下一行正文」合并成
        # ``## 年假员工入职满一年后……``，这一行仍以 ``## `` 开头 ⇒ 被 :data:`_HEADING`
        # 当成标题，而标题只用来填 ``heading_path`` ⇒ 正文被静默丢弃。
        # 触发条件是「标题以 CJK 结尾 且 下一行以 CJK 开头」—— 中文文档的常态。
        lines = normalize_text(text, join_cjk_lines=False).split("\n")
        index = 0
        while index < len(lines):
            line = lines[index]

            if _FENCE.match(line):
                flush()
                fence = _FENCE.match(line)
                marker = fence.group(1) if fence else "```"
                block = [line]
                index += 1
                while index < len(lines):
                    block.append(lines[index])
                    if lines[index].strip().startswith(marker):
                        index += 1
                        break
                    index += 1
                # 代码块原子：缩进与换行都是语义的一部分
                builder.add("\n".join(block), heading_path=path(), atomic=True)
                continue

            if _TABLE_ROW.match(line):
                flush()
                block = []
                while index < len(lines) and (
                    _TABLE_ROW.match(lines[index]) or lines[index].strip() == ""
                ):
                    block.append(lines[index])
                    index += 1
                rows = [row for row in block if row.strip()]
                if len(rows) >= _MIN_TABLE_ROWS:
                    builder.add("\n".join(rows), heading_path=path(), atomic=True)
                else:
                    paragraph.extend(block)
                continue

            heading = _HEADING.match(line)
            if heading:
                flush()
                level = len(heading.group(1))
                title = heading.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                index += 1
                continue

            paragraph.append(line)
            index += 1

        flush()
        return builder.build(encoding=encoding, heading_count=len(stack))


__all__ = ["MarkdownParser"]
