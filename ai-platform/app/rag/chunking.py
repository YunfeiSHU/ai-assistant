"""Chunk 切分（``docs/06`` §4.3，``REQ-RAG-005``）。

长度度量固定为 token 数而非字符数：中文一页 A4 约 800–1200 汉字但 token 数差异很大，
按字符切会系统性破坏语义。

「相邻 chunk < ``chunk_size × 0.3`` 时 MUST 合并」与 ``AC-RAG-07``「无 chunk 长度 >
``chunk_size × 1.5``」两条要求靠合并上限 = ``chunk_size × 1.5`` 合起来：没有这个上限，
把 100 token 碎片并进 512 token 正常块会得到 612 token 的超长块。

**UP-03（2026-10-02）：合并的「触发阈值」与「填充目标」是两件事，此前被混为一谈。**
原实现用 ``chunk_size × 0.3`` 同时当两者 —— 一旦累到 153 token 就停止合并，于是碎段文档
的有效块长被钉在配额的 34%（8MB 实测中位 175 / 配置 512），片数被放大 2.6 倍
（16,969 vs 整篇一次切的 6,527），并因此撞上 ``MAX_DOC_CHUNKS=10000`` 丢掉 41% 正文。
现在 ``SHORT_CHUNK_RATIO`` 只表达「低于它 MUST 合并」的语义下限，真正决定停在哪儿的是
:data:`MERGE_FILL_RATIO`；两者是超集关系，那条 MUST 依旧成立。
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

#: 短块合并的触发阈值（占 ``chunk_size`` 的比例）：低于它的块 MUST 与后一块合并。
#: 它不再决定合并「停在哪里」（那是 :data:`MERGE_FILL_RATIO` 的事），保留它是因为
#: 「必须合并」这条语义需要一个可引用的数字，而实际合并范围是它的超集。
SHORT_CHUNK_RATIO = 0.3

#: 短块合并的填充目标（占 ``chunk_size`` 的比例）：合并到「再加一块就超它」为止。
#: 为什么不是 0.3：碎片文档会「一累到 153 就停」，片数被放大 2.6 倍并触发
#: ``MAX_DOC_CHUNKS`` 静默截断（见模块文档 UP-03）。
#: 警告：改这个值是改召回配方（召回粒度、上下文预算、向量条数一起变），
#: 必须连同 ``AC-RAG-*`` 与召回评测一起重定标（见 ``ai-platform-go/docs/10`` §8-2）。
MERGE_FILL_RATIO = 1.0

#: 单块长度硬上限（占 ``chunk_size`` 的比例，``AC-RAG-07``）
MAX_CHUNK_RATIO = 1.5

#: 表格 / 代码块允许超过 ``chunk_size`` 的倍数；超过之后才做内部切分
ATOMIC_SPLIT_RATIO = 2.0


@dataclass(slots=True, frozen=True)
class SourceBlock:
    """切分输入：一段带位置信息的连续文本。

    ``offset`` 是该文本在整篇归一化正文中的字符偏移，用来产出可复现的
    ``char_start``/``char_end``。PDF 没有「整篇连续正文」的概念，但仍可由各页
    拼接得到一个稳定坐标 —— 只要解析器给出一致的 ``offset``。
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
            # 关键：分隔符留在前一块末尾。默认行为是把 ``。`` 放到后一块开头，
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

    @property
    def merge_target_tokens(self) -> int:
        """短块合并的填充目标：合并到「再加一块就超它」为止。

        取 ``min(MERGE_FILL_RATIO × chunk_size, max_chunk_tokens)``：
        这样无论两个比例怎么配，合并结果都不可能突破 ``AC-RAG-07`` 的硬上限。
        """
        return min(int(self.chunk_size * MERGE_FILL_RATIO), self.max_chunk_tokens)

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
        """相邻短块向后合并，填到接近 ``chunk_size`` 才停（UP-03）。

        只合并同一位置上下文（同 ``page`` 且同 ``heading_path``）的相邻块：跨标题或
        跨页合并会把两节内容缝在一起，且合并后的 ``page`` 无从取值，引用溯源会指错地方。
        """
        if not drafts:
            return []
        target = self.merge_target_tokens
        out: list[ChunkDraft] = []
        for draft in drafts:
            previous = out[-1] if out else None
            if (
                previous is not None
                and previous.page == draft.page
                and previous.heading_path == draft.heading_path
                # 还没填满 && 并进来也不会超配额（超了就让 draft 单独成块）
                and previous.token_count < target
                and previous.token_count + draft.token_count <= target
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
    "MERGE_FILL_RATIO",
    "SEPARATORS",
    "SHORT_CHUNK_RATIO",
    "ChunkDraft",
    "ChunkingService",
    "SourceBlock",
]
