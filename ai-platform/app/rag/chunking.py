"""Chunk 切分（``docs/06`` §4.3，``REQ-RAG-005``）。

长度度量固定为 **token 数**而不是字符数：中文一页 A4 约 800–1200 汉字但 token 数
差异很大，按字符切会系统性破坏语义。

两条看似冲突的要求在这里是怎么合起来的：

* 「相邻 chunk < ``chunk_size × 0.3`` 时 MUST 与后一块合并」——避免碎片；
* ``AC-RAG-07``「无 chunk 长度 > ``chunk_size × 1.5``」——避免超长。

合并的上限就取 ``chunk_size × 1.5``：这既是长度硬上限，也是「短块必须合并」与
「块不能太长」两条约束的交点。没有这个上限，把 100 token 的碎片并进 512 token 的
正常块会得到 612 token 的超长块，反而违反验收标准。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.tokens import count_tokens

#: 分隔符优先级（docs/06 §4.3）。末位空串是 langchain 的「逐字符切」约定，
#: 保证任何极端输入（一坨没有标点的长串）也能切到目标长度。
SEPARATORS: tuple[str, ...] = (
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    "，",
    " ",
    "",
)

#: 短块合并阈值（占 ``chunk_size`` 的比例）
SHORT_CHUNK_RATIO = 0.3

#: 单块长度硬上限（占 ``chunk_size`` 的比例，``AC-RAG-07``）
MAX_CHUNK_RATIO = 1.5

#: 表格 / 代码块允许超过 ``chunk_size`` 的倍数；超过之后才做内部切分
ATOMIC_SPLIT_RATIO = 2.0


@dataclass(slots=True, frozen=True)
class SourceBlock:
    """切分输入：一段带位置信息的连续文本。

    ``offset`` 是该文本在**整篇归一化正文**中的字符偏移，用来产出可复现的
    ``char_start``/``char_end``。PDF 没有「整篇连续正文」的概念，但仍可由
    「各页拼起来」得到一个稳定坐标——只要解析器给出一致的 ``offset``，
    引用溯源里的字符区间就有意义。
    """

    text: str
    offset: int = 0
    page: int | None = None
    heading_path: str = ""
    #: ``True`` = 表格 / 代码块，整体保留，不跨块切分
    atomic: bool = False


@dataclass(slots=True)
class ChunkDraft:
    """待落库的切片（尚未分配 ``chunk_id``）。"""

    content: str
    chunk_index: int
    char_start: int
    char_end: int
    token_count: int
    page: int | None = None
    heading_path: str = ""
    metadata: dict[str, object] = field(default_factory=dict)
    #: 是否由相邻短块合并而来（排障时能看出切分器做了什么）
    merged: bool = False


class ChunkingService:
    """按 token 递归切分，并按 ``docs/06`` §4.3 做短块合并与原子块保护。"""

    def __init__(self, *, chunk_size: int, chunk_overlap: int) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap 必须满足 0 <= overlap < chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=count_tokens,
            separators=list(SEPARATORS),
            # **关键**：分隔符留在前一块末尾。默认行为是把 ``。`` 放到后一块开头，
            # 直接违反「MUST NOT 在中文句末标点之前断开」与 AC-RAG-07。
            keep_separator="end",
            add_start_index=True,
            strip_whitespace=True,
        )

    # ------------------------------------------------------------------
    @property
    def max_chunk_tokens(self) -> int:
        """单块长度硬上限（``AC-RAG-07``）。"""
        return int(self.chunk_size * MAX_CHUNK_RATIO)

    def split_blocks(self, blocks: Sequence[SourceBlock]) -> list[ChunkDraft]:
        """把带位置信息的文本块切成切片草稿。"""
        drafts: list[ChunkDraft] = []
        for block in blocks:
            drafts.extend(self._split_block(block))
        merged = self._merge_short(drafts)
        for index, draft in enumerate(merged):
            draft.chunk_index = index
        return merged

    # ------------------------------------------------------------------
    def _split_block(self, block: SourceBlock) -> list[ChunkDraft]:
        text = block.text.strip()
        if not text:
            return []
        # 原子块：整体保留；只有超过 chunk_size × 2 才允许内部切分（§4.3）
        if block.atomic and count_tokens(text) <= int(self.chunk_size * ATOMIC_SPLIT_RATIO):
            return [self._draft(block, text, block.offset, block.offset + len(text))]

        pieces = self._splitter.create_documents([text])
        drafts: list[ChunkDraft] = []
        for piece in pieces:
            content = piece.page_content.strip()
            if not content:
                # 空白 chunk 在切分阶段丢弃，绝不能让空文本进入 Embedding
                continue
            start = block.offset + int(piece.metadata.get("start_index", 0))
            drafts.append(self._draft(block, content, start, start + len(content)))
        if not drafts:
            return []
        if block.atomic:
            # 原子块被内部切分后，逐块标记，便于排障时知道这不是普通段落
            for draft in drafts:
                draft.metadata["atomic"] = True
        return drafts

    def _draft(self, block: SourceBlock, content: str, start: int, end: int) -> ChunkDraft:
        return ChunkDraft(
            content=content,
            chunk_index=0,
            char_start=start,
            char_end=end,
            token_count=count_tokens(content),
            page=block.page,
            heading_path=block.heading_path,
        )

    def _merge_short(self, drafts: list[ChunkDraft]) -> list[ChunkDraft]:
        """相邻短块向后合并。

        只合并 **同一位置上下文**（同 ``page`` 且同 ``heading_path``）的相邻块：
        跨标题或跨页合并会把两节内容缝在一起，且合并后的 ``page`` 无从取值，
        引用溯源就会指错地方。
        """
        if not drafts:
            return []
        floor = self.chunk_size * SHORT_CHUNK_RATIO
        ceiling = self.max_chunk_tokens
        out: list[ChunkDraft] = []
        for draft in drafts:
            previous = out[-1] if out else None
            if (
                previous is not None
                and previous.page == draft.page
                and previous.heading_path == draft.heading_path
                and previous.token_count < floor
                and previous.token_count + draft.token_count <= ceiling
            ):
                separator = self._joiner(previous.content, draft.content)
                previous.content = f"{previous.content}{separator}{draft.content}"
                previous.char_end = max(previous.char_end, draft.char_end)
                previous.token_count = count_tokens(previous.content)
                previous.merged = True
                continue
            out.append(draft)
        return out

    @staticmethod
    def _joiner(left: str, right: str) -> str:
        """合并时的连接符：中文之间不加空格，英文之间补一个空格。"""
        if not left or not right:
            return ""
        if left[-1].isspace() or right[0].isspace():
            return ""
        if _is_cjk(left[-1]) or _is_cjk(right[0]):
            return ""
        return " "


def _is_cjk(char: str) -> bool:
    return "\u2e80" <= char <= "\u9fff" or "\uff00" <= char <= "\uffef"


__all__ = [
    "ATOMIC_SPLIT_RATIO",
    "MAX_CHUNK_RATIO",
    "SEPARATORS",
    "SHORT_CHUNK_RATIO",
    "ChunkDraft",
    "ChunkingService",
    "SourceBlock",
]
