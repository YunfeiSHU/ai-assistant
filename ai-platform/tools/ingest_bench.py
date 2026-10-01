"""入库**切分**链路的本地基准（UP-03 切分配方），走真实代码路径。

只做一件事：拿 ``--mb`` 大小的正文跑一遍真实入库的**前两段**，逐段计时并给出
新旧合并条件的对比：

* 解析 ``parse_document`` / 切分 ``_split_block`` / 合并 ``_merge_short`` / 逐块 sha256
  / 向量化（**hash 档**，不联网）/ upsert；
* 旧实现用一个内联复刻（``old_merge_short``，停止条件 ``0.3``）算，
  所以一次运行就能给出 before/after 两个数，也顺带自证探针没写错。

⚠️ 测量纪律：本机回环有沙箱噪声，**计时必须在同一次 shell 调用内完成**；
跑基准时不要同时跑别的重活，否则数字不可比。

📌 这里**曾经还有两组测量**（B：真实 BGE 的块形状成本；C：``torch_num_threads``
对同机其它进程的影响）。2026-10-02 换成云端 provider 后它们已无法运行 ——
embedding 走硅基流动 API，本地 HF 权重（``BAAI/bge-m3`` / ``bge-reranker-v2-m3``，
共 8.5GB）与 torch 都不再在链路上，故连同代码一起删除；历史数字保留在
``ai-platform-go/docs/11-优化项实测记录.md``（§6.5/§6.6）与 ``docs/09/10``。

用法：
    python tools/ingest_bench.py --mb 8
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tests.conftest import build_settings  # noqa: E402

from app.core.text import sha256_hex  # noqa: E402
from app.core.tokens import count_tokens  # noqa: E402
from app.rag.chunking import ChunkDraft, ChunkingService  # noqa: E402
from app.rag.parsers import parse_document  # noqa: E402

LINE = "# 上传流式转发测试\n\n这是第 {0} 段占位文本，用于把文件撑到指定大小。\n"


def make_text(target_bytes: int) -> bytes:
    line_bytes = len(LINE.format(0).encode("utf-8"))
    raw = "".join(LINE.format(i) for i in range(math.ceil(target_bytes / line_bytes))).encode(
        "utf-8"
    )
    if len(raw) > target_bytes:
        cut = min(target_bytes, len(raw) - 1)
        while cut > 0 and raw[cut] != 10:
            cut -= 1
        raw = raw[: cut + 1]
    return raw


def old_merge_short(service: ChunkingService, drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    """UP-03 之前的实现：0.3 当**停止条件**，1.5 倍当上限。"""
    if not drafts:
        return []
    floor = service.chunk_size * 0.3
    ceiling = service.max_chunk_tokens
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
            previous.content = f"{previous.content}{draft.content}"
            previous.char_end = max(previous.char_end, draft.char_end)
            previous.token_count = count_tokens(previous.content)
            previous.merged = True
            continue
        out.append(draft)
    return out


def stats(name: str, drafts: list[ChunkDraft]) -> dict:
    lens = sorted(d.token_count for d in drafts)
    med = lens[len(lens) // 2]
    total = sum(lens)
    print(
        f"   {name}: chunks={len(drafts)} 总token={total} 中位={med} "
        f"min={lens[0]} max={lens[-1]} 填充率={med / 512:.0%}"
    )
    return {"chunks": len(drafts), "tokens": total, "median": med}


def _fake_chunk(draft: ChunkDraft, index: int):
    """把 draft 包成 ``Chunk``（只为走 ``_to_vector_payload`` 的真实代码路径）。"""
    from app.rag.service import Chunk

    return Chunk(
        id=f"chk_{index}",
        doc_id="doc_probe",
        kb_id="kb_probe",
        user_id="u_probe",
        chunk_index=index,
        content=draft.content,
        content_sha256=sha256_hex(draft.content),
        char_start=draft.char_start,
        char_end=draft.char_end,
        token_count=draft.token_count,
        page=draft.page,
        heading_path=draft.heading_path,
        created_at="2026-10-01T00:00:00Z",
        metadata={
            "doc_name": "m5.txt",
            "chunk_size": 512,
            "chunk_overlap": 64,
            "merged": draft.merged,
        },
    )


def part_a(size_mb: float) -> None:
    print(f"=== A. 入库分阶段 A/B（{size_mb:g}MB，真实 parse_document + ChunkingService）===")
    raw = make_text(int(size_mb * 1024 * 1024))
    print(f"   输入 {len(raw)} bytes")

    t0 = time.perf_counter()
    parsed = parse_document("m5.txt", raw, min_chars=1)
    t_parse = time.perf_counter() - t0
    print(f"   parse_document      : {t_parse:6.2f}s  blocks={len(parsed.blocks)}")

    service = ChunkingService(chunk_size=512, chunk_overlap=64)
    t0 = time.perf_counter()
    drafts: list[ChunkDraft] = []
    for block in parsed.blocks:
        drafts.extend(service._split_block(block))
    t_split = time.perf_counter() - t0
    print(f"   _split_block ×84k  : {t_split:6.2f}s  drafts={len(drafts)}")

    arms: dict[str, list[ChunkDraft]] = {}
    t0 = time.perf_counter()
    arms["OLD(0.3 停止, 1.5×上限)"] = old_merge_short(service, copy.deepcopy(drafts))
    t_old = time.perf_counter() - t0
    t0 = time.perf_counter()
    arms["NEW(填到 chunk_size)"] = service._merge_short(copy.deepcopy(drafts))
    t_new = time.perf_counter() - t0
    print(f"   _merge_short 旧条件 : {t_old:6.2f}s")
    print(f"   _merge_short 新条件 : {t_new:6.2f}s")

    info = {}
    for name, chunks in arms.items():
        info[name] = stats(name, chunks)

    # 逐块 sha256（_build_chunks 里的同步 CPU；每块一次）
    for name, chunks in arms.items():
        t0 = time.perf_counter()
        for chunk in chunks:
            sha256_hex(chunk.content)
        print(f"   sha256 逐块 {name}: {time.perf_counter() - t0:.2f}s")

    # 向量化 + upsert：与 _embed_and_upsert 同形（batch=16，hash 向量化，内存 upsert）
    settings = build_settings()
    from app.rag.embedding import build_embedding_provider
    from app.rag.embedding.base import embed_texts
    from app.rag.service import _to_vector_payload
    from app.rag.vectorstore.memory import InMemoryVectorStore

    provider = build_embedding_provider(settings)
    batch_size = settings.embedding_batch_size

    async def embed_and_upsert(chunks: list[ChunkDraft]) -> tuple[float, float]:
        store = InMemoryVectorStore(dim=settings.embedding_dim)
        drafts_as_chunks = []
        for index, draft in enumerate(chunks):
            draft.chunk_index = index
            drafts_as_chunks.append(_fake_chunk(draft, index))
        t_embed = t_upsert = 0.0
        for start in range(0, len(drafts_as_chunks), batch_size):
            batch = drafts_as_chunks[start : start + batch_size]
            t0 = time.perf_counter()
            vectors = await embed_texts(provider, [c.content for c in batch])
            t_embed += time.perf_counter() - t0
            t0 = time.perf_counter()
            await store.upsert([_to_vector_payload(c) for c in batch], vectors)
            t_upsert += time.perf_counter() - t0
        return t_embed, t_upsert

    for name, chunks in arms.items():
        t_embed, t_upsert = asyncio.run(embed_and_upsert(chunks))
        total = t_embed + t_upsert
        print(
            f"   embed+upsert(hash) {name}: 合计 {total:.2f}s "
            f"(embed {t_embed:.2f}s + upsert {t_upsert:.2f}s, "
            f"{total / len(chunks) * 1000:.3f} ms/块, {len(chunks) / batch_size:.0f} 批)"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="入库切分基准（真实代码路径）")
    ap.add_argument("--mb", type=float, default=8.0, help="正文大小（MB）")
    args = ap.parse_args()
    part_a(args.mb)


if __name__ == "__main__":
    main()
