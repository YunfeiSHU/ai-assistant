"""入库**向量化 + upsert** 的真实档基准（云端 provider，按生产并发形态）。

为什么需要它：``tools/ingest_bench.py`` 只覆盖「解析 + 切分 + 合并」，向量化走的是
**hash 档**（不联网）。而换成云端 provider 之后，入库的绝大部分耗时都在 embedding 上
（``tests/eval`` 实测占 **96.8%**），真正需要基准的正是这一段 —— 本脚本补上它。

与 ``IngestionService._embed_and_upsert`` 同形：按 ``INGEST_EMBED_WINDOW`` 组窗口，
窗口内 ``asyncio.gather`` 并发提交、窗口内按批顺序 upsert。**串行提交会严重低估吞吐**：
``docs/06`` §4.5 实测 8MB / 5,821 片走硅基流动，串行 98.2s、窗口并发约 40.4s，
差的 6 倍全在等待上。

用法（在 ``ai-platform/`` 下执行）：

    python tools/ingest_bench_cloud.py --mb 3                 # 云端档（读 .env）
    python tools/ingest_bench_cloud.py --mb 3 --embed hash    # 离线对照，不联网

⚠️ 测量纪律：计时必须在同一次调用内完成，跑基准时不要同时跑别的重活。
本脚本已关掉 embedding 缓存 —— 语料是重复生成的，开着缓存会让耗时假性变快。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from dotenv import dotenv_values  # noqa: E402
from tests.conftest import build_settings  # noqa: E402

from app.core.text import sha256_hex  # noqa: E402
from app.core.tokens import count_tokens  # noqa: E402
from app.rag.chunking import ChunkingService  # noqa: E402
from app.rag.embedding import build_embedding_provider  # noqa: E402
from app.rag.embedding.base import embed_texts  # noqa: E402
from app.rag.parsers import parse_document  # noqa: E402
from app.rag.service import Chunk, _to_vector_payload  # noqa: E402
from app.rag.vectorstore.memory import InMemoryVectorStore  # noqa: E402

#: 与 ingest_bench 同形的填充语料，保证两次基准的块形状可比。
LINE = "# 上传流式转发测试\n\n这是第 {0} 段占位文本，用于把文件撑到指定大小。\n"


def make_text(target_bytes: int) -> bytes:
    """生成指定大小的正文（与 ``tools/ingest_bench.py`` 完全一致的生成器）。"""
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


def build_settings_for(embed: str):
    """内存档 + 关缓存；``cloud`` 走 .env 的硅基流动，``hash`` 走离线档。

    只改必要的 kwargs，不改 ``os.environ`` —— 本模块虽不被 pytest 收集，
    但环境变量污染是上一轮已经踩过的坑（见 ``tests/eval/run_eval.py`` 的注释）。
    """
    common = {
        "infra_backend": "memory",
        "embedding_dim": 1024,
        "milvus_vector_dim": 1024,
        "metrics_enabled": False,
        "otel_enabled": False,
        "metrics_port": 0,
        "task_runner": "inline",
        "embedding_cache_enabled": False,
        "chunk_size": 512,
        "chunk_overlap": 64,
    }
    if embed == "hash":
        return build_settings(**common)

    env = dotenv_values(HERE.parent / ".env")
    key = env.get("SILICONFLOW_API_KEY")
    if not key:
        raise SystemExit("--embed cloud 需要 .env 里的 SILICONFLOW_API_KEY")
    return build_settings(
        **common,
        embedding_provider="siliconflow",
        embedding_model=env.get("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B"),
        siliconflow_api_key=key,
        siliconflow_base_url=env.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
    )


async def run(size_mb: float, embed: str) -> None:
    settings = build_settings_for(embed)
    provider = build_embedding_provider(settings)
    store = InMemoryVectorStore(dim=settings.embedding_dim)

    # 预热 tiktoken：cl100k 词表懒加载，首次 count_tokens 要读 BPE（约 3s）。
    # 不预热，这笔一次性成本会全记在第一段的切分上。
    count_tokens("预热")

    raw = make_text(int(size_mb * 1024 * 1024))
    print(f"=== 入库基准（{size_mb:g}MB，embed={embed}）===")
    print(f"   输入 {len(raw)} bytes；provider={type(provider).__name__}")
    print(
        f"   batch={settings.embedding_batch_size} window={settings.ingest_embed_window} "
        f"→ 每窗口最多 {settings.embedding_batch_size * settings.ingest_embed_window} 片在飞"
    )

    t0 = time.perf_counter()
    parsed = parse_document("bench.txt", raw, min_chars=1)
    t_parse = time.perf_counter() - t0
    print(f"   parse_document     : {t_parse:6.2f}s  blocks={len(parsed.blocks)}")

    chunker = ChunkingService(chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap)
    t0 = time.perf_counter()
    drafts = chunker.split_blocks(parsed.blocks)
    t_split = time.perf_counter() - t0
    lens = sorted(d.token_count for d in drafts)
    print(
        f"   split+merge        : {t_split:6.2f}s  chunks={len(drafts)} "
        f"中位={lens[len(lens) // 2]} max={lens[-1]}"
    )

    t0 = time.perf_counter()
    chunks = [
        Chunk(
            id=f"chk_{i:05d}",
            doc_id="doc_bench",
            kb_id="kb_bench",
            user_id="u_bench",
            chunk_index=i,
            content=d.content,
            content_sha256=sha256_hex(d.content),
            char_start=d.char_start,
            char_end=d.char_end,
            token_count=d.token_count,
            page=d.page,
            heading_path=d.heading_path,
            created_at="2026-10-07T00:00:00Z",
            metadata={"doc_name": "bench.txt", "merged": d.merged},
        )
        for i, d in enumerate(drafts)
    ]
    t_hash = time.perf_counter() - t0
    total_tokens = sum(c.token_count for c in chunks)
    print(f"   build+sha256       : {t_hash:6.2f}s  tokens={total_tokens}")

    # ---- 向量化 + upsert：与 IngestionService._embed_and_upsert 同形 ----
    batch_size = max(1, settings.embedding_batch_size)
    window = max(1, settings.ingest_embed_window)
    total = len(chunks)
    t_upsert = 0.0
    t0 = time.perf_counter()
    for window_start in range(0, total, batch_size * window):
        batches = [
            chunks[start : start + batch_size]
            for start in range(
                window_start, min(window_start + batch_size * window, total), batch_size
            )
        ]
        vectors_per_batch = await asyncio.gather(
            *(embed_texts(provider, [chunk.content for chunk in batch]) for batch in batches)
        )
        for batch, vectors in zip(batches, vectors_per_batch, strict=True):
            t1 = time.perf_counter()
            await store.upsert([_to_vector_payload(c) for c in batch], vectors)
            t_upsert += time.perf_counter() - t1
    t_embed_upsert = time.perf_counter() - t0
    t_embed = t_embed_upsert - t_upsert

    grand = t_parse + t_split + t_hash + t_embed_upsert
    print(
        f"   embed+upsert(窗口)  : {t_embed_upsert:6.2f}s  （embed {t_embed:.2f}s + upsert {t_upsert:.2f}s）"
    )
    print(f"   ---- 合计 {grand:.2f}s")
    print(
        f"   吞吐：{total / grand:7.1f} 片/s ｜ {len(raw) / 1024 / 1024 / grand:5.2f} MB/s ｜ "
        f"{grand / total * 1000:5.1f} ms/片"
    )
    print(f"   向量化占合计 {t_embed / grand:.1%}；upsert 占 {t_upsert / grand:.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description="入库向量化 + upsert 基准（云端档走生产并发形态）")
    ap.add_argument("--mb", type=float, default=3.0, help="正文大小（MB）")
    ap.add_argument(
        "--embed", choices=("cloud", "hash"), default="cloud", help="cloud=硅基流动；hash=离线对照"
    )
    args = ap.parse_args()
    asyncio.run(run(args.mb, args.embed))


if __name__ == "__main__":
    main()
