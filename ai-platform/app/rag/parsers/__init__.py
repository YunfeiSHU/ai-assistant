"""解析器注册与分发（``docs/06`` §3.2 / §4.1）。

**扩展名与魔数必须同时校验**，不一致直接 ``415``。只信扩展名等于让用户决定
我们怎么解析：把 ``.exe`` 改名成 ``.pdf`` 会走到 PDF 解析器并抛出难以理解的
底层异常，而魔数校验能在读第一行时就给出明确结论。
"""

from __future__ import annotations

import os

from app.core.exceptions import AppError, ErrorCode
from app.rag.parsers.base import (
    BlockBuilder,
    DocumentParser,
    ParsedDocument,
    build_blocks_from_pages,
)
from app.rag.parsers.docx import DocxParser
from app.rag.parsers.html import HtmlParser
from app.rag.parsers.markdown import MarkdownParser
from app.rag.parsers.pdf import PdfParser
from app.rag.parsers.text import TextParser

#: 扩展名 → MIME（``docs/06`` §3.2 允许列表）
EXTENSION_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html",
    ".htm": "text/html",
}

#: 二进制魔数（前缀 → 描述）。文本格式没有魔数，只能靠「有没有撞上别的魔数」反向判断。
_MAGIC_NUMBERS: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"{\\rtf", "rtf"),
    (b"\xd0\xcf\x11\xe0", "ole"),
    (b"\x7fELF", "elf"),
    (b"MZ", "pe"),
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF8", "gif"),
)

#: 各扩展名允许的文件类型（``zip`` 既可能是 docx 也可能是别的压缩包）
_ALLOWED_KINDS: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"pdf"}),
    ".docx": frozenset({"zip"}),
    ".md": frozenset({"text"}),
    ".markdown": frozenset({"text"}),
    ".txt": frozenset({"text"}),
    ".html": frozenset({"text"}),
    ".htm": frozenset({"text"}),
}

_PARSERS: tuple[DocumentParser, ...] = (
    PdfParser(),
    DocxParser(),
    MarkdownParser(),
    HtmlParser(),
    TextParser(),
)


def _parser_map() -> dict[str, DocumentParser]:
    mapping: dict[str, DocumentParser] = {}
    for parser in _PARSERS:
        for extension in parser.extensions:
            mapping[extension] = parser
    return mapping


PARSERS: dict[str, DocumentParser] = _parser_map()


def sniff_kind(raw: bytes) -> str:
    """识别文件类型：命中魔数则返回其名称，否则视为 ``text``。"""
    for magic, kind in _MAGIC_NUMBERS:
        if raw.startswith(magic):
            return kind
    return "text"


def normalize_extension(filename: str) -> str:
    """取小写扩展名（含点）。"""
    return os.path.splitext(filename or "")[1].lower()


def detect_mime(filename: str) -> str:
    """由扩展名推断 MIME；未知扩展名返回 ``application/octet-stream``。"""
    return EXTENSION_MIME.get(normalize_extension(filename), "application/octet-stream")


def get_parser(filename: str, raw: bytes) -> DocumentParser:
    """按扩展名 + 魔数选出解析器。

    Raises:
        AppError: ``415 UNSUPPORTED_FILE_TYPE``（扩展名不支持或与内容不符）。
    """
    extension = normalize_extension(filename)
    parser = PARSERS.get(extension)
    if parser is None:
        raise AppError(
            ErrorCode.UNSUPPORTED_FILE_TYPE,
            f"不支持的文件类型：{extension or filename}",
            {"allowed": sorted(PARSERS)},
        )
    kind = sniff_kind(raw)
    allowed = _ALLOWED_KINDS.get(extension, frozenset({"text"}))
    if kind not in allowed:
        raise AppError(
            ErrorCode.UNSUPPORTED_FILE_TYPE,
            f"文件内容（{kind}）与扩展名（{extension}）不符",
            {"extension": extension, "detected": kind, "allowed": sorted(allowed)},
        )
    return parser


def parse_document(filename: str, raw: bytes, *, min_chars: int) -> ParsedDocument:
    """解析并做「有效正文长度」校验（``docs/06`` §3.2）。

    Raises:
        AppError: ``422 UNPROCESSABLE_DOCUMENT``（有效文本不足 ``min_chars``）。
    """
    parser = get_parser(filename, raw)
    parsed = parser.parse(raw, filename=filename)
    if parsed.is_empty(min_chars=min_chars):
        raise AppError(
            ErrorCode.UNPROCESSABLE_DOCUMENT,
            f"文档有效文本不足（{parsed.char_count} < {min_chars} 字符）",
            {"filename": filename, "char_count": parsed.char_count},
        )
    return parsed


__all__ = [
    "EXTENSION_MIME",
    "PARSERS",
    "BlockBuilder",
    "DocumentParser",
    "DocxParser",
    "HtmlParser",
    "MarkdownParser",
    "ParsedDocument",
    "PdfParser",
    "TextParser",
    "build_blocks_from_pages",
    "detect_mime",
    "get_parser",
    "normalize_extension",
    "parse_document",
    "sniff_kind",
]
