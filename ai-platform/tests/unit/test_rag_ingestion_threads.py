"""单元测试：入库链路的重型 CPU 步骤必须**离开事件循环**。

背景（``ai-platform-go/docs/09`` §2.5）：8MB 上传会在入库时跑三段纯 CPU 的同步代码
（解析 / 切分 / 逐块哈希）。它们如果写在协程体里，就会占住事件循环几十秒 ——
这期间同一个进程连 ``/health`` 都不应答，网关的就绪探测会把「正在解析」误判成
「AI 挂了」，整轮验收跟着崩。

断言方式刻意不用「计时」（本机只剩 ~1.7GB 可用，时序断言必然间歇性假红），
而是直接断言**执行这三段代码的线程不是事件循环线程** —— 这是「不阻塞事件循环」
的充分条件，且完全确定。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

import pytest
from tests.conftest import build_settings

from app.infrastructure.storage.base import Chunk, Document
from app.main import build_rag_services
from app.rag import service as service_module
from app.rag.chunking import ChunkDraft, ChunkingService, SourceBlock
from app.rag.parsers import ParsedDocument
from app.rag.service import IngestionService

#: 足够切出多块、又不必等太久的正文（``min_doc_chars`` 默认 50）
_BODY = "".join(f"第 {index} 段用于验证入库链路的线程归属。\n" for index in range(200))


async def test_ingest_heavy_cpu_steps_run_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析 / 切分 / 哈希三段都必须在工作线程里跑。"""
    settings = build_settings(task_runner="none")  # 只建任务，不自动执行
    services = build_rag_services(settings)
    tasks = services["tasks"]

    kb = await services["kb_service"].create(user_id="u_loop", payload={"name": "kb-loop"})
    uploaded = await services["document_service"].upload(
        kb_id=kb.id, user_id="u_loop", text=_BODY, doc_name="loop.txt"
    )
    task = await tasks.get(uploaded.task_id)

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    real_parse = service_module.parse_document

    def _spy_parse(name: str, raw: bytes, *, min_chars: int) -> ParsedDocument:
        seen["parse"] = threading.get_ident()
        return real_parse(name, raw, min_chars=min_chars)

    real_split = ChunkingService.split_blocks

    def _spy_split(self: ChunkingService, blocks: Sequence[SourceBlock]) -> list[ChunkDraft]:
        seen["chunk"] = threading.get_ident()
        return real_split(self, blocks)

    real_build = IngestionService._build_chunks

    def _spy_build(
        self: IngestionService, document: Document, drafts: Sequence[ChunkDraft]
    ) -> list[Chunk]:
        seen["hash"] = threading.get_ident()
        return real_build(self, document, drafts)

    monkeypatch.setattr(service_module, "parse_document", _spy_parse)
    monkeypatch.setattr(ChunkingService, "split_blocks", _spy_split)
    monkeypatch.setattr(IngestionService, "_build_chunks", _spy_build)

    await services["ingestion"]._ingest_document(task)

    assert set(seen) == {"parse", "chunk", "hash"}, "三段都必须被走到（否则断言形同虚设）"
    for stage, ident in seen.items():
        assert ident != loop_thread, f"{stage} 跑在事件循环线程上，会阻塞整个进程"
