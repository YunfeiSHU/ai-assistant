"""文本清洗与编码探测单测（``docs/06`` §4.1）。

清洗的验收标准不是「看起来干净」，而是**让切分与检索稳定**：页码/页眉页脚这类
噪声会占 token 预算，还会在相邻切片里反复出现，导致「检索命中页眉」。所以这里
重点测两类容易做错的边界：

* **该删的删干净**：零宽字符、页码行、跨页页眉；
* **不该删的别碰**：正文里的数字、行首缩进（代码块语义）、短标题行。
"""

from __future__ import annotations

import codecs

import pytest

from app.core.text import (
    clean_pages,
    decode_bytes,
    detect_repeated_lines,
    normalize_text,
    sha256_hex,
    strip_page_numbers,
    strip_repeated_lines,
)

# ---------------------------------------------------------------------------
# normalize_text
# ---------------------------------------------------------------------------


def test_normalize_removes_zero_width_characters() -> None:
    """零宽字符必须删：它们让「相同内容」的哈希不同、检索匹配失败。"""
    raw = "退款\u200b时效\ufeff为\u00ad7天\ufeff"

    assert normalize_text(raw) == "退款时效为7天"


def test_normalize_unifies_line_endings() -> None:
    """``\\r\\n`` 与 ``\\r`` 都归一成 ``\\n``（否则行数统计与切分全乱）。"""
    assert normalize_text("a\r\nb\rc") == "a\nb\nc"


def test_normalize_applies_nfc() -> None:
    """组合字符归一成 NFC：``e`` + 组合尖音符 与 ``é`` 必须是同一个字。"""
    decomposed = "e\u0301"
    assert normalize_text(decomposed) == "\u00e9"


def test_normalize_fixes_hyphen_line_break() -> None:
    """PDF 抽取常留「行末连字符 + 换行」，必须拼回去。"""
    assert normalize_text("inter-\nnational") == "international"


def test_normalize_keeps_real_hyphen_break() -> None:
    """只修小写字母开头的情况，``-\\n`` 后接大写/数字不当断词。"""
    assert normalize_text("A-\nB") == "A-\nB"


def test_normalize_merges_cjk_line_breaks() -> None:
    """句中被视觉换行切开的 CJK 要合并（这是 PDF 抽取最常见的「孤行」）。"""
    assert normalize_text("退款时效为\n七个自然日") == "退款时效为七个自然日"


def test_normalize_keeps_cjk_digit_line_breaks() -> None:
    """CJK 与数字之间的换行**不**合并（刻意的保守选择）。

    合并这一侧很容易把「正文 + 单独成行的页码」也粘起来，而页码行必须先能被
    ``strip_page_numbers`` 单独识别出来。代价是 ``退款时效为\n7 个自然日`` 会留下
    一个换行 —— 它不影响阅读，也不会把两段内容混成一段。
    """
    assert normalize_text("退款时效为\n7 个自然日") == "退款时效为\n7 个自然日"


def test_normalize_merges_across_sentence_end() -> None:
    """句末标点之后仍然合并：PDF 的硬换行位置与语义边界无关。"""
    assert normalize_text("第一句。\n第二句") == "第一句。第二句"


def test_normalize_does_not_merge_latin_lines() -> None:
    """纯英文行不合并 —— 英文的换行往往是真实的排版结构。"""
    assert normalize_text("hello\nworld") == "hello\nworld"


def test_normalize_collapses_blank_lines() -> None:
    """连续空行压成一个（页码被删掉后会留下大量空行）。"""
    assert normalize_text("a\n\n\n\n\nb") == "a\n\nb"


def test_normalize_strips_trailing_space_but_keeps_indent() -> None:
    """只去行尾空白：行首缩进是代码块/列表的语义，不能动。"""
    assert normalize_text("    if x:   \n        pass  ") == "    if x:\n        pass"


def test_normalize_does_not_touch_fullwidth_or_case() -> None:
    """刻意不做全角/半角转换与大小写折叠：会改变代码与数字的语义。"""
    assert normalize_text("ＡＢＣ 1.0") == "ＡＢＣ 1.0"


def test_normalize_empty_input() -> None:
    """空串返回空串（不要返回 ``"\\n"`` 之类）。"""
    assert normalize_text("") == ""
    assert normalize_text("\u200b\ufeff") == ""


# ---------------------------------------------------------------------------
# strip_page_numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    ["- 3 -", "— 3 —", "第 3 页", "第 3 页 / 共 10 页", "Page 3", "Page 3 of 10", "12", "  7  "],
)
def test_strip_page_numbers_removes_known_shapes(line: str) -> None:
    """页码的常见写法都要能识别。"""
    assert strip_page_numbers([line]) == []


@pytest.mark.parametrize(
    "line",
    [
        "第 3 页的表格说明了退款时效",  # 页码样式出现在句子里
        "共 10 个工作日",  # 只有「第 N 页」才算
        "2026 年 9 月更新",  # 正文里的数字
        "第3章 概述",  # 章节号不是页码
        "Pageant 展示",  # 前缀相似但不匹配
    ],
)
def test_strip_page_numbers_keeps_body_text(line: str) -> None:
    """**不能误删正文**：只删「整行且极短」的纯页码。"""
    assert strip_page_numbers([line]) == [line]


def test_strip_page_numbers_keeps_order_and_other_lines() -> None:
    """保留非页码行的相对顺序。"""
    lines = ["标题", "- 1 -", "正文", "2", "结尾"]

    assert strip_page_numbers(lines) == ["标题", "正文", "结尾"]


# ---------------------------------------------------------------------------
# detect_repeated_lines / strip_repeated_lines
# ---------------------------------------------------------------------------


def _page(body: str, header: str = "内部资料 请勿外传") -> str:
    return f"{header}\n{body}\n第 1 页"


def test_detect_repeated_lines_finds_header_footer() -> None:
    """跨页重复的页眉/页脚要被识别出来。"""
    pages = [_page(f"正文 {index}") for index in range(5)]

    repeated = detect_repeated_lines(pages)

    assert "内部资料 请勿外传" in repeated
    assert "正文 0" not in repeated


def test_detect_repeated_lines_needs_at_least_three_pages() -> None:
    """1-2 页谈「跨页重复」没有统计意义，直接返回空集。"""
    assert detect_repeated_lines([_page("a"), _page("b")]) == set()


def test_detect_repeated_lines_ignores_long_lines() -> None:
    """超长行不参与判定：把重复的长段落当页眉删掉会丢正文。"""
    long_line = "很长的正文" * 60
    pages = [f"页眉\n{long_line}" for _ in range(6)]

    repeated = detect_repeated_lines(pages, max_chars=120)

    assert repeated == {"页眉"}


def test_detect_repeated_lines_ratio_is_relative() -> None:
    """阈值按比例算：10 页里出现 3 次（30%）在 ratio=0.6 时不算重复。"""
    pages = [f"偶发页眉\n正文{index}" for index in range(10)]
    # 只有前 3 页带这一行
    pages = [("偶发页眉\n正文" if index < 3 else "正文") + str(index) for index in range(10)]

    assert detect_repeated_lines(pages, ratio=0.6) == set()
    assert detect_repeated_lines(pages, ratio=0.3) == {"偶发页眉"}


def test_detect_repeated_lines_counts_each_line_once_per_page() -> None:
    """同一页里重复出现多次的行只算一次（否则单页内重复就能刷过阈值）。"""
    pages = [f"重复行\n重复行\n重复行\n唯一{index}" for index in range(4)]

    repeated = detect_repeated_lines(pages, ratio=0.6)

    assert repeated == {"重复行"}, "每页不同的「唯一N」不在重复集合里"


def test_strip_repeated_lines_removes_matching_lines() -> None:
    """按 ``strip()`` 后的内容匹配删除，保留其余行。"""
    pages = ["页眉\n正文 A", "页眉\n正文 B"]

    result = strip_repeated_lines(pages, {"页眉"})

    assert result == ["正文 A", "正文 B"]


def test_strip_repeated_lines_noop_on_empty_set() -> None:
    """空集合是「没识别到页眉」的常见情况，必须原样返回。"""
    pages = ["a", "b"]

    assert strip_repeated_lines(pages, set()) == pages


# ---------------------------------------------------------------------------
# clean_pages（端到端）
# ---------------------------------------------------------------------------


def test_clean_pages_removes_noise_and_keeps_body() -> None:
    """一次清洗要把零宽字符、页码、页眉全处理掉，正文一字不少。"""
    pages = [
        "产品手册\u200b\n退款时效为 7 个自然日\n- 1 -\n第 1 页",
        "产品手册\n申请入口在订单页\n- 2 -\n第 2 页",
        "产品手册\n审核通过后原路退回\n- 3 -\n第 3 页",
    ]

    cleaned = clean_pages(pages)

    assert len(cleaned) == 3
    joined = "\n".join(cleaned)
    assert "退款时效为 7 个自然日" in joined
    assert "产品手册" not in joined, "跨页重复的页眉应被删除"
    assert "\u200b" not in joined
    assert "第 1 页" not in joined


def test_clean_pages_can_keep_page_numbers() -> None:
    """``remove_page_numbers=False`` 时页码保留（排查解析问题时有用）。"""
    pages = ["正文\n第 1 页"]

    assert "第 1 页" in clean_pages(pages, remove_page_numbers=False)[0]


def test_clean_pages_removes_cjk_only_header() -> None:
    """**回归用例**：纯中文页眉必须能被删掉。

    以前 ``clean_pages`` 先做 ``normalize_text``（里面会合并 CJK 孤行），于是
    「产品手册\\n退款时效…」被粘成一行，`detect_repeated_lines` 再也看不到
    「产品手册」这个独立行 —— 中文文档的页眉/页脚清洗等于完全失效。
    """
    pages = [f"产品手册\n正文第{index}条" for index in range(4)]

    cleaned = clean_pages(pages)

    assert all("产品手册" not in page for page in cleaned)
    assert cleaned == [f"正文第{index}条" for index in range(4)]


def test_clean_pages_keeps_cjk_body_lines_merged() -> None:
    """页眉删完之后，正文的 CJK 孤行仍要合并（清洗不能把功能一起删掉）。"""
    pages = [
        "产品手册\n退款时效为\n七个自然日",
        "产品手册\n申请入口在\n订单页",
        "产品手册\n审核通过后\n原路退回",
    ]

    cleaned = clean_pages(pages)

    assert cleaned[0] == "退款时效为七个自然日"
    assert cleaned[1] == "申请入口在订单页"


def test_clean_pages_is_idempotent() -> None:
    """清洗两次与一次结果相同：否则「重新入库」会产出不同的切片。"""
    pages = ["页眉\n正文\u200b\n- 1 -", "页眉\n正文\n- 2 -", "页眉\n正文\n- 3 -"]

    once = clean_pages(pages)

    assert clean_pages(once) == once


# ---------------------------------------------------------------------------
# sha256_hex
# ---------------------------------------------------------------------------


def test_sha256_hex_str_and_bytes_agree() -> None:
    """``str`` 与 ``bytes`` 入参必须同哈希（引用溯源要用同一个值做校验）。"""
    assert sha256_hex("退款") == sha256_hex("退款".encode())


def test_sha256_hex_is_64_char_and_differs() -> None:
    """64 位 hex 且内容不同哈希不同。"""
    first = sha256_hex("政策 A")
    second = sha256_hex("政策 B")

    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")
    assert first != second


def test_sha256_hex_known_vector() -> None:
    """对空串的已知结果，确认不是自造的哈希。"""
    assert sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# ---------------------------------------------------------------------------
# decode_bytes
# ---------------------------------------------------------------------------


def test_decode_bytes_plain_utf8_reports_utf8() -> None:
    """无 BOM 的 UTF-8 要报 ``utf-8``（不是 ``utf-8-sig``）。"""
    text, encoding = decode_bytes("退款政策".encode())

    assert text == "退款政策"
    assert encoding == "utf-8"


def test_decode_bytes_bom_is_stripped_and_reported() -> None:
    """带 BOM 的文件要吃掉 BOM 并报 ``utf-8-sig``。"""
    raw = codecs.BOM_UTF8 + "退款政策".encode()

    text, encoding = decode_bytes(raw)

    assert text == "退款政策", "BOM 不能进正文（否则第一段永远带一个不可见字符）"
    assert not text.startswith("\ufeff")
    assert encoding == "utf-8-sig"


def test_decode_bytes_gb18030_fallback() -> None:
    """GBK/GB2312 超集走 GB18030 回退。"""
    raw = "退款政策".encode("gb18030")

    text, encoding = decode_bytes(raw)

    assert text == "退款政策"
    assert encoding == "gb18030"


def test_decode_bytes_never_raises_on_garbage() -> None:
    """非法字节兜底成替换字符，而不是抛异常（半个乱码字符好过一个 500）。"""
    text, encoding = decode_bytes(b"\xff\xfe\x00\x01abc")

    assert encoding in {"gb18030", "utf-8/replace"}
    assert "abc" in text


def test_decode_bytes_empty_input() -> None:
    """空文件返回空文本，不报错。"""
    assert decode_bytes(b"") == ("", "utf-8")
