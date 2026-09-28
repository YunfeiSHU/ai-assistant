"""切分器单测（``docs/06`` §4.3，``AC-RAG-07``）。

断言的都是**可被违反的硬约束**：中文句末标点前不切、单块不超硬上限、
原子块不被拆、短块按同页同标题合并、字符区间能对上原文。
这些性质一旦坏掉，表现是「检索结果看起来还行，但引用位置全错」。
"""

from __future__ import annotations

from app.core.tokens import count_tokens
from app.rag.chunking import ChunkingService, SourceBlock

PARAGRAPHS = (
    "退款申请需在收到货物后 7 个工作日内提交。\n\n"
    "审核通过后 3 个工作日内原路退回。\n\n"
    "商品需保持完好，附件齐全。\n\n"
    "换货需提供质量问题凭证。\n\n"
    "发票抬头修改需要提交工单。\n\n"
    "运费由买家承担，质量问题除外。\n"
)


def test_no_split_before_chinese_terminator() -> None:
    """MUST NOT 在中文句末标点之前断开（``keep_separator="end"`` 的作用）。"""
    service = ChunkingService(chunk_size=32, chunk_overlap=4)

    drafts = service.split_blocks([SourceBlock(text=PARAGRAPHS)])

    assert len(drafts) > 1, "这段文本应当被切成多片，否则没验到切分行为"
    for draft in drafts:
        assert draft.content.strip(), "不允许产出空片"
        assert not draft.content.lstrip().startswith(("。", "！", "？", "，")), draft.content


def test_chunk_size_hard_cap() -> None:
    """单块不得超过 ``chunk_size × 1.5``（``AC-RAG-07``）。"""
    service = ChunkingService(chunk_size=64, chunk_overlap=8)

    drafts = service.split_blocks([SourceBlock(text=PARAGRAPHS * 3)])

    assert drafts
    for draft in drafts:
        assert draft.token_count <= service.max_chunk_tokens, draft.content[:50]


def test_short_chunks_are_merged_within_same_position() -> None:
    """同页同标题的相邻短块会被合并（避免产生一堆碎片）。"""
    service = ChunkingService(chunk_size=256, chunk_overlap=16)
    blocks = [
        SourceBlock(text="第一句很短。", offset=0, page=1, heading_path="A"),
        SourceBlock(text="第二句也很短。", offset=20, page=1, heading_path="A"),
    ]

    drafts = service.split_blocks(blocks)

    assert len(drafts) == 1, "两段短文本应当合并成一片，否则检索会被碎片淹没"
    assert drafts[0].merged is True
    assert "第一句很短。" in drafts[0].content
    assert "第二句也很短。" in drafts[0].content


def test_short_chunks_are_not_merged_across_headings() -> None:
    """标题不同（跨章节）时**不**合并：合并会让引用指到错误的章节。"""
    service = ChunkingService(chunk_size=256, chunk_overlap=16)
    blocks = [
        SourceBlock(text="退款说明。", offset=0, heading_path="售后 > 退款"),
        SourceBlock(text="换货说明。", offset=10, heading_path="售后 > 换货"),
    ]

    drafts = service.split_blocks(blocks)

    assert len(drafts) == 2
    assert {draft.heading_path for draft in drafts} == {"售后 > 退款", "售后 > 换货"}


def test_atomic_block_kept_whole() -> None:
    """表格 / 代码块整体保留，不按标点切开（切开会破坏结构）。"""
    service = ChunkingService(chunk_size=32, chunk_overlap=4)
    table = "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |"

    drafts = service.split_blocks([SourceBlock(text=table, atomic=True)])

    assert len(drafts) == 1
    assert drafts[0].content.strip() == table


def test_oversized_atomic_block_is_split() -> None:
    """超过 ``chunk_size × 2`` 的原子块仍然要切，否则会污染上下文预算。"""
    service = ChunkingService(chunk_size=32, chunk_overlap=4)
    huge = "\n".join(f"| 行{i} | 值{i} |" for i in range(60))

    drafts = service.split_blocks([SourceBlock(text=huge, atomic=True)])

    assert len(drafts) > 1
    for draft in drafts:
        assert draft.token_count <= service.max_chunk_tokens


def test_char_offsets_track_the_source() -> None:
    """``char_start``/``char_end`` 必须能回指原文（引用溯源的根基）。"""
    text = "第一段内容比较长。" * 5 + "\n\n" + "第二段内容也比较长。" * 5
    service = ChunkingService(chunk_size=32, chunk_overlap=4)

    drafts = service.split_blocks([SourceBlock(text=text)])

    assert drafts
    for draft in drafts:
        assert draft.char_start >= 0
        assert draft.char_end > draft.char_start
        assert draft.token_count == count_tokens(draft.content)
    assert drafts[0].char_start == 0


def test_page_and_heading_are_inherited() -> None:
    """切片必须继承来源块的页码与标题路径（否则引用卡片没法定位）。"""
    service = ChunkingService(chunk_size=64, chunk_overlap=8)

    drafts = service.split_blocks(
        [SourceBlock(text=PARAGRAPHS, offset=100, page=3, heading_path="售后 > 退款 > 时效")]
    )

    assert drafts
    assert all(draft.page == 3 for draft in drafts)
    assert all(draft.heading_path == "售后 > 退款 > 时效" for draft in drafts)


def test_chunk_index_is_sequential() -> None:
    """``chunk_index`` 必须从 0 连续递增：它是「相邻合并」与游标的依据。"""
    service = ChunkingService(chunk_size=32, chunk_overlap=4)

    drafts = service.split_blocks([SourceBlock(text=PARAGRAPHS * 2)])

    assert [draft.chunk_index for draft in drafts] == list(range(len(drafts)))


def test_empty_input_yields_nothing() -> None:
    """空输入不能产出空切片（空切片会变成「检索得到一段空白」）。"""
    service = ChunkingService(chunk_size=128, chunk_overlap=16)

    assert service.split_blocks([]) == []
    assert service.split_blocks([SourceBlock(text="   ")]) == []
