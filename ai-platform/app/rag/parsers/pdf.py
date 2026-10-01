"""PDF 解析（``pypdf``）。

``docs/06`` §4.1：保留页码，因为引用溯源要能指向「第几页」。无文本层的扫描件必须判为
不可处理（``422``），而不是返回空文本后「入库成功但检索不到」。
"""

from __future__ import annotations

from pypdf import PdfReader

from app.core.exceptions import AppError, ErrorCode
from app.core.text import clean_pages
from app.rag.parsers.base import ParsedDocument, build_blocks_from_pages


class PdfParser:
    """``.pdf`` 解析器。"""

    extensions = (".pdf",)

    def parse(self, raw: bytes, *, filename: str) -> ParsedDocument:
        import io

        try:
            reader = PdfReader(io.BytesIO(raw))
            page_count = len(reader.pages)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.UNPROCESSABLE_DOCUMENT,
                f"PDF 解析失败：{exc}",
                {"filename": filename},
            ) from exc

        if page_count == 0:
            raise AppError(
                ErrorCode.UNPROCESSABLE_DOCUMENT,
                "PDF 不含任何页面",
                {"filename": filename},
            )

        raw_pages: list[str] = []
        failed = 0
        for page in reader.pages:
            try:
                raw_pages.append(page.extract_text() or "")
            except Exception:
                failed += 1
                raw_pages.append("")
        if failed and failed == page_count:
            raise AppError(
                ErrorCode.UNPROCESSABLE_DOCUMENT,
                "PDF 全部页面解析失败",
                {"filename": filename, "page_count": page_count},
            )

        pages = clean_pages(raw_pages)
        blocks, text, char_count = build_blocks_from_pages(pages)
        if not text.strip():
            # 扫描版 PDF：能读页面但抽不出文字
            raise AppError(
                ErrorCode.UNPROCESSABLE_DOCUMENT,
                "PDF 无文本层（可能是扫描件），请先做 OCR",
                {"filename": filename, "page_count": page_count},
            )
        return ParsedDocument(
            blocks=blocks,
            page_count=page_count,
            char_count=char_count,
            text=text,
            metadata={"failed_pages": failed} if failed else {},
        )


__all__ = ["PdfParser"]
