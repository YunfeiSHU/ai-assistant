"""文本清洗与编码探测（``docs/06`` §4.1）。

清洗的目标不是「变干净」，而是让切分与检索稳定：页码、页眉页脚、零宽字符这些噪声会
占据 token 预算，并在相邻切片里反复出现导致「检索命中页眉」。
"""

from __future__ import annotations

import codecs
import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

#: 零宽与方向控制字符：肉眼不可见，但会让「相同内容」的哈希不同、检索匹配失败
_ZERO_WIDTH = dict.fromkeys(
    [
        0x200B,  # ZERO WIDTH SPACE
        0x200C,  # ZERO WIDTH NON-JOINER
        0x200D,  # ZERO WIDTH JOINER
        0x2060,  # WORD JOINER
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
        0x00AD,  # SOFT HYPHEN
    ]
)

#: 常见的软换行修正：PDF 抽取常把「行末连字符 + 换行」留下
_HYPHEN_BREAK = re.compile(r"([A-Za-z])-\n([a-z])")

#: 中文排版里的「孤行」：PDF 抽取把一段话按视觉行切开、句中被强插换行。
#: 只要换行两侧都是 CJK 就合并（包括句末标点之后：PDF 的硬换行位置与语义边界无关）。
#: 刻意不合并「CJK + 非 CJK」的组合（如 ``退款时效为\n7 个自然日``）：那一侧很容易把
#: 「正文 + 单独成行的页码」也粘起来，而页码行必须先能被 :func:`strip_page_numbers` 单独识别。
_CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
_CJK_LINEBREAK = re.compile(rf"([{_CJK}])[ \t]*\n[ \t]*([{_CJK}])")

_BLANK_LINES = re.compile(r"\n{3,}")

#: 页码行：``- 3 -`` / ``第 3 页`` / ``Page 3 of 10`` / 单独一个数字
_PAGE_NUMBER_LINE = re.compile(
    r"^\s*(?:[-—–]\s*\d+\s*[-—–]|第\s*\d+\s*页(?:\s*/\s*共\s*\d+\s*页)?|"
    r"page\s+\d+(?:\s+of\s+\d+)?|\d{1,4})\s*$",
    re.IGNORECASE,
)


def _normalize_preserving_lines(text: str) -> str:
    """只做不破坏行结构的归一化：零宽字符、换行符、NFC、断词、行末空白。

    归一化拆成两步：``_CJK_LINEBREAK`` 会把行合并掉，而 :func:`strip_page_numbers` /
    :func:`detect_repeated_lines` 都依赖「一行就是一个独立语义单元」。顺序一旦反过来，
    纯中文的页眉（如「产品手册」）会被粘到下一行正文上，从此再也无法被识别为重复行 ——
    中文文档的页眉页脚清洗直接失效。所以：保留行结构 → 行级清洗 → 最后才合并行。
    """
    cleaned = text.translate(_ZERO_WIDTH)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = unicodedata.normalize("NFC", cleaned)
    cleaned = _HYPHEN_BREAK.sub(r"\1\2", cleaned)
    # 只在行尾去空白：行首缩进对代码/列表有意义
    return "\n".join(line.rstrip() for line in cleaned.split("\n"))


def normalize_text(text: str, *, join_cjk_lines: bool = True) -> str:
    """归一化：去零宽字符、统一换行、修连字符断词、合并 CJK 孤行、压缩空行。

    刻意不做的事：不删空格、不做全角/半角互转、不做大小写折叠 —— 这些会改变代码块与
    数字的语义（``1.0`` 与 ``1.O`` 只差一次折叠）。本函数会合并行，因此行级清洗必须在
    它之前（见 :func:`clean_pages`）。

    Args:
        join_cjk_lines: 是否合并中文孤行。**Markdown 必须传 ``False``** —— 它的行结构
            是语义（``## 标题`` 独占一行），合并会把「``## 年假`` + 下一行正文」粘成
            一行 ``## 年假员工入职……``；这一行仍以 ``## `` 开头，于是被当成标题、正文
            被静默丢弃（全文如此则 ``char_count=0``，报"有效文本不足"）。
    """
    if not text:
        return ""
    cleaned = _normalize_preserving_lines(text)
    if join_cjk_lines:
        cleaned = _CJK_LINEBREAK.sub(r"\1\2", cleaned)
    # 只裁剪首尾的空行：用 ``strip()`` 会把首行缩进一起吃掉（与「保留行首缩进」矛盾）
    return _BLANK_LINES.sub("\n\n", cleaned).strip("\n")


def strip_page_numbers(lines: Sequence[str]) -> list[str]:
    """删除「单独成行的页码」——只在行极短且完全匹配时删，避免误删正文里的数字。"""
    return [line for line in lines if not _PAGE_NUMBER_LINE.match(line)]


def detect_repeated_lines(
    pages: Sequence[str], *, ratio: float = 0.6, max_chars: int = 120
) -> set[str]:
    """找出跨页重复的行（页眉/页脚）。

    ``ratio`` 是「出现在多少比例的页里才算重复」的阈值 —— 用比例而不是绝对次数：
    10 页的文档里出现 5 次和 100 页里出现 5 次含义完全不同。行长度上限则是防止把重复的
    短标题行（如「注意事项」）当成页眉删掉，那种误删会丢信息。
    """
    if len(pages) < 3:
        # 1-2 页谈「跨页重复」没有统计意义
        return set()
    counter: Counter[str] = Counter()
    for page in pages:
        seen = {line.strip() for line in page.split("\n") if 0 < len(line.strip()) <= max_chars}
        counter.update(seen)
    threshold = max(2, int(len(pages) * ratio))
    return {line for line, count in counter.items() if count >= threshold}


def strip_repeated_lines(pages: Sequence[str], repeated: set[str]) -> list[str]:
    """从每页里删掉识别出的页眉/页脚行。"""
    if not repeated:
        return list(pages)
    return [
        "\n".join(line for line in page.split("\n") if line.strip() not in repeated)
        for page in pages
    ]


def clean_pages(pages: Sequence[str], *, remove_page_numbers: bool = True) -> list[str]:
    """按页清洗：去零宽字符、修断词、删页眉页脚与页码。

    顺序是这段逻辑里最容易搞错的地方，固定为：「保留行结构的归一化」→「删页码行」→
    「识别并删跨页重复行」→「合并 CJK 孤行」。把最后的合并步骤提前会让中文页眉粘进
    正文，之后再也无法识别为重复行。
    """
    prepared = [_normalize_preserving_lines(page) for page in pages]
    if remove_page_numbers:
        prepared = ["\n".join(strip_page_numbers(page.split("\n"))) for page in prepared]
    repeated = detect_repeated_lines(prepared)
    cleaned = strip_repeated_lines(prepared, repeated)
    return [normalize_text(page) for page in cleaned]


def sha256_hex(data: bytes | str) -> str:
    """内容哈希（``docs/06`` §3.1 用``content_sha256`` 做去重与引用溯源）。"""
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """探测文本编码，返回 ``(文本, 编码名)``。

    顺序刻意如此：先看 BOM（那时必须用 ``utf-8-sig`` 把 BOM 吃掉），再试无 BOM 的 UTF-8，
    最后 GB18030（兼容 GBK/GB2312）。不用 ``chardet`` 之类的统计探测：中文短文本的统计
    信号很弱，误判会把整篇文档变成乱码且很难发现。

    按 BOM 显式分流的原因：``utf-8-sig`` 对无 BOM 的文件同样能解码，早先的实现会让**所有**
    UTF-8 文件都报成 ``utf-8-sig``。这个字段会落到文档元信息里给排障用，报错编码名会把
    人带偏。
    """
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        # 最后兜底：替换非法字节而不是失败——半个乱码字符好过一个 500
        return raw.decode("utf-8", errors="replace"), "utf-8/replace"


__all__ = [
    "clean_pages",
    "decode_bytes",
    "detect_repeated_lines",
    "normalize_text",
    "sha256_hex",
    "strip_page_numbers",
    "strip_repeated_lines",
]
