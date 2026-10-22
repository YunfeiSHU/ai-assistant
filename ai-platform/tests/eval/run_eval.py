"""RAG 检索质量 + 链路耗时评测（走真实代码路径）。

背景：``docs/11-§5``（``REQ-TEST-004``）早就规定了 RAG 效果评估的数据集口径与门槛，
但 ``tests/eval/`` 一直不存在、也没有任何用例挂 ``pytest.mark.eval`` ——
于是「没有检索评测集」成了文档自己承认的最大流程缺口，任何动召回粒度的改动都停在
「能改、不敢改」。本脚本把 §5 的口径真正落地。

测什么（每段都走生产代码，不自建复刻）：

* 入库：``parse_document`` → ``ChunkingService.split_blocks``（split + merge）
  → 逐块 ``sha256_hex`` → ``embed_texts`` → ``VectorStore.upsert``，逐段计时；
* 检索：``Retriever.retrieve_detailed``，同一批查询跑**两个臂**并对比：
  - ``vector`` 臂：``with_rerank=False``，按向量余弦排序（对应 §5.2 的「仅向量召回」）；
  - ``rerank`` 臂：``with_rerank=True``，按交叉编码器排序、只保留 ``rerank_top_n``；
* 质量：``hit@k`` 与 ``MRR``，并按 §5.2 的门槛给出 PASS/FAIL；
* 耗时：两个臂的 ``elapsed_ms`` 分位，以及重排段的近似增量。

与 §5.1 的一处**有意偏差**：§5.1 要求标注 ``gold_chunk_ids``，本数据集标注的是
``gold_spans``（gold 片段里的原文字串）+ ``gold_doc_ids``。原因是 ``chunk_id`` 由
切片参数派生，改一次 ``CHUNK_SIZE`` 全部失效，指标就不可比了；用「原文字串是否出现
在命中片段里」判定，切片参数怎么变都成立。判定前把两侧空白全部去掉，避免相邻合并
引入的换行造成漏判。

用法（在 ``ai-platform/`` 下执行）：

    python tests/eval/run_eval.py                 # 云端 embedding + 重排（读 .env）
    python tests/eval/run_eval.py --mode hash     # 离线确定性基线（hash 向量化 + 关重排）
    python tests/eval/run_eval.py --limit 5 -v    # 冒烟
    python tests/eval/run_eval.py --json out.json # 落盘结果

⚠️ 环境纪律：``.env`` 里是 ``INFRA_BACKEND=real``。本脚本只度量检索质量，Milvus /
MySQL 不是被测对象，因此强制压到内存档 —— 否则连不上 Milvus 会让评测变成「测环境」。
手段是**显式 kwargs**（见 ``make_settings``），而不是改 ``os.environ``：
pydantic-settings 的优先级是「init kwargs > 环境变量 > .env」，kwargs 已足够压过 .env，
而改环境变量会污染 pytest 会话（原因写在下面 import 段的注释里）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent.parent  # ai-platform/
sys.path.insert(0, str(PKG_ROOT))

# 注意：这里**刻意不写** ``os.environ[...] = ...``。本模块会被 pytest 收集
# （见同目录 test_rag_eval.py），import 期的 os.environ 赋值会污染整个测试会话 ——
# 实测写入 METRICS_ENABLED=false 后，tests/contract/test_observability.py 有 6 个
# 用例集体失败（conftest 的进程环境快照把被污染的值当成了基线）。压内存档改成在
# make_settings() 里用显式 kwargs：pydantic-settings 的优先级是
# 「init kwargs > 环境变量 > .env」，kwargs 足以压过 .env 的 real 档。

from dotenv import dotenv_values  # noqa: E402
from tests.conftest import build_settings  # noqa: E402

from app.core.text import sha256_hex  # noqa: E402
from app.core.tokens import count_tokens  # noqa: E402
from app.rag.chunking import ChunkingService  # noqa: E402
from app.rag.embedding import build_embedding_provider  # noqa: E402
from app.rag.embedding.base import embed_texts  # noqa: E402
from app.rag.parsers import parse_document  # noqa: E402
from app.rag.reranker import build_reranker  # noqa: E402
from app.rag.retriever import Retriever  # noqa: E402
from app.rag.service import Chunk, _to_vector_payload  # noqa: E402
from app.rag.vectorstore import build_vector_store  # noqa: E402

CORPUS_DIR = HERE / "corpus"
DATASET_PATH = HERE / "dataset.jsonl"
USER_ID = "u_eval"
KB_ID = "kb_eval"

#: 评测的 k 取值。@20 对应生产默认 top_k。
KS = (1, 3, 5, 10, 20)

#: docs/11-§5.2 的验收门槛（回归保护线，不是质量上限）。
GATES = {
    "recall@5": 0.85,  # 重排臂：top-5 命中任一 gold 的问题占比
    "recall@20": 0.92,  # 仅向量召回阶段
    "MRR": 0.70,  # 重排臂
    "rerank_gain": 0.05,  # recall@5(重排) - recall@5(仅向量)
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def norm(text: str) -> str:
    """判定用的归一化：去掉所有空白并转小写。"""
    return "".join(text.split()).lower()


def pct(values: list[float], p: float) -> float:
    """线性插值分位（样本量小，不引第三方依赖）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def make_settings(mode: str) -> Any:
    """构造评测配置。

    ``build_settings`` 的默认档就是「hash 向量化 + 关重排 + inline + 内存向量库」，
    两个模式都从它出发只改必要字段，保证评测与测试套件看到同一套默认值。
    """
    common: dict[str, Any] = {
        "infra_backend": "memory",  # 覆盖 .env 的 real
        "embedding_dim": 1024,
        "milvus_vector_dim": 1024,
        "metrics_port": 0,
        "metrics_enabled": False,
        "otel_enabled": False,
        "task_runner": "inline",
        # 关缓存：否则第二个臂的查询向量直接命中缓存，重排段增量会被算成 0
        "embedding_cache_enabled": False,
        "chunk_size": 512,
        "chunk_overlap": 64,
        "rag_merge_adjacent_chunks": True,
    }
    if mode == "hash":
        return build_settings(**common)

    env = dotenv_values(PKG_ROOT / ".env")
    key = env.get("SILICONFLOW_API_KEY")
    if not key:
        raise SystemExit("--mode cloud 需要 .env 里的 SILICONFLOW_API_KEY")
    return build_settings(
        **common,
        embedding_provider="siliconflow",
        embedding_model=env.get("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B"),
        siliconflow_api_key=key,
        siliconflow_base_url=env.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        reranker_enabled=True,
        reranker_provider="siliconflow",
        reranker_model=env.get("RERANKER_MODEL", "Qwen/Qwen3-Reranker-0.6B"),
        reranker_top_n=int(env.get("RERANKER_TOP_N", "5")),
    )


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------
@dataclass
class IngestReport:
    docs: int = 0
    chunks: int = 0
    tokens: int = 0
    truncated_docs: list[str] = field(default_factory=list)
    parse_s: float = 0.0
    chunk_s: float = 0.0
    hash_s: float = 0.0
    embed_s: float = 0.0
    upsert_s: float = 0.0
    embed_calls: int = 0
    per_doc: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_s(self) -> float:
        return self.parse_s + self.chunk_s + self.hash_s + self.embed_s + self.upsert_s


async def ingest(settings: Any, provider: Any, store: Any, *, verbose: bool) -> IngestReport:
    """走真实入库链路把一个语料目录灌进向量库，并逐段计时。"""
    chunker = ChunkingService(chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap)
    batch_size = settings.embedding_batch_size
    report = IngestReport()
    all_chunks: list[Chunk] = []

    # 预热 tiktoken：cl100k 词表是懒加载的，首次 count_tokens 要读 BPE 文件（实测约 3s）。
    # 不预热，这笔一次性装载成本会整笔记在第一份文档的切分上，让 split+merge 看起来慢两个数量级。
    count_tokens("预热")

    for path in sorted(p for p in CORPUS_DIR.iterdir() if p.is_file()):
        raw = path.read_bytes()

        t0 = time.perf_counter()
        parsed = parse_document(path.name, raw, min_chars=settings.min_doc_chars)
        report.parse_s += time.perf_counter() - t0

        # split_blocks 是公开入口：内部就是 _split_block + _merge_short
        t0 = time.perf_counter()
        drafts = chunker.split_blocks(parsed.blocks)
        report.chunk_s += time.perf_counter() - t0

        if len(drafts) > settings.max_doc_chunks:
            report.truncated_docs.append(path.name)
            drafts = drafts[: settings.max_doc_chunks]

        t0 = time.perf_counter()
        doc_id = path.stem  # 与 dataset.jsonl 的 gold_doc_ids 同口径
        for index, draft in enumerate(drafts):
            all_chunks.append(
                Chunk(
                    id=f"{doc_id}#{index:03d}",
                    doc_id=doc_id,
                    kb_id=KB_ID,
                    user_id=USER_ID,
                    chunk_index=index,
                    content=draft.content,
                    content_sha256=sha256_hex(draft.content),
                    char_start=draft.char_start,
                    char_end=draft.char_end,
                    token_count=draft.token_count,
                    page=draft.page,
                    heading_path=draft.heading_path,
                    created_at="2026-10-07T00:00:00Z",
                    metadata={
                        "doc_name": path.name,
                        "chunk_size": settings.chunk_size,
                        "chunk_overlap": settings.chunk_overlap,
                        "merged": draft.merged,
                    },
                )
            )
        report.hash_s += time.perf_counter() - t0

        report.docs += 1
        report.per_doc.append(
            {
                "doc": path.name,
                "bytes": len(raw),
                "chunks": len(drafts),
                "tokens": sum(d.token_count for d in drafts),
            }
        )
        if verbose:
            print(
                f"    {path.name:<26} chunks={len(drafts):>2} "
                f"tokens={sum(d.token_count for d in drafts):>5}"
            )

    report.chunks = len(all_chunks)
    report.tokens = sum(c.token_count for c in all_chunks)

    # 向量化 + upsert，与 IngestionService._embed_and_upsert 同形（分批批量向量化）
    for start in range(0, len(all_chunks), batch_size):
        batch = all_chunks[start : start + batch_size]
        t0 = time.perf_counter()
        vectors = await embed_texts(provider, [c.content for c in batch])
        report.embed_s += time.perf_counter() - t0
        report.embed_calls += 1
        t0 = time.perf_counter()
        await store.upsert([_to_vector_payload(c) for c in batch], vectors)
        report.upsert_s += time.perf_counter() - t0

    return report


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
@dataclass
class ArmResult:
    name: str
    hits: dict[int, int]
    mrr: float
    latencies: list[float]
    rows: list[dict[str, Any]]
    unanswered: int
    doc_hits: int  # 命中 gold_doc_ids 的问题数（doc 级召回，诊断用）


async def run_arm(
    retriever: Retriever,
    queries: list[dict[str, Any]],
    *,
    settings: Any,
    with_rerank: bool,
    top_k: int,
) -> ArmResult:
    """跑一个臂。

    关重排时把 ``rerank_top_n`` 设为 ``top_k``：``Retriever._rerank`` 在关闭重排时
    仍按 ``rerank_top_n`` 截断，不设等于测不出 @10/@20。
    """
    rerank_top_n = settings.reranker_top_n if with_rerank else top_k
    hits = dict.fromkeys(KS, 0)
    latencies: list[float] = []
    rows: list[dict[str, Any]] = []
    reciprocal: list[float] = []
    unanswered = 0
    doc_hits = 0

    for q in queries:
        result = await retriever.retrieve_detailed(
            query=q["query"],
            user_id=USER_ID,
            kb_ids=[KB_ID],
            top_k=top_k,
            rerank_top_n=rerank_top_n,
            score_threshold=settings.score_threshold,
            with_rerank=with_rerank,
        )
        latencies.append(float(result.elapsed_ms))
        if not result.items:
            unanswered += 1

        spans = [norm(s) for s in q.get("gold_spans", [])]
        gold_docs = set(q.get("gold_doc_ids", []))
        texts = [norm(item.text) for item in result.items]

        first_rank = 0
        for rank, text in enumerate(texts, start=1):
            if spans and any(span in text for span in spans):
                first_rank = rank
                break

        if spans:
            reciprocal.append(1.0 / first_rank if first_rank else 0.0)
            for k in KS:
                if first_rank and first_rank <= k:
                    hits[k] += 1
            if any(item.doc_id in gold_docs for item in result.items):
                doc_hits += 1

        rows.append(
            {
                "id": q["id"],
                "type": q["type"],
                "query": q["query"],
                "recalled": result.recalled,
                "returned": len(result.items),
                "rerank_used": result.rerank_used,
                "first_rank": first_rank or None,
                "elapsed_ms": result.elapsed_ms,
                # 负例诊断：无 gold 时看系统给出的最高分，用于判断阈值该定在哪
                "top_score": round(result.items[0].score, 4) if result.items else None,
                "top_vector_score": round(result.items[0].vector_score, 4)
                if result.items
                else None,
                "top_doc": result.items[0].doc_id if result.items else None,
            }
        )

    return ArmResult(
        name="rerank" if with_rerank else "vector",
        hits=hits,
        mrr=(sum(reciprocal) / len(reciprocal)) if reciprocal else 0.0,
        latencies=latencies,
        rows=rows,
        unanswered=unanswered,
        doc_hits=doc_hits,
    )


def print_arm(arm: ArmResult, scored: int) -> None:
    print(f"\n  [{arm.name} 臂]  有效查询 {scored} 条（负例不计入）")
    print(
        "    " + "  ".join(f"hit@{k:<2}={arm.hits[k] / scored if scored else 0:6.1%}" for k in KS)
    )
    print(f"    MRR={arm.mrr:.4f}   doc级命中={arm.doc_hits}/{scored}   返回空={arm.unanswered}")
    lat = arm.latencies
    print(
        f"    耗时 p50={pct(lat, 0.50):7.1f}ms  p90={pct(lat, 0.90):7.1f}ms  "
        f"p99={pct(lat, 0.99):7.1f}ms  mean={statistics.fmean(lat) if lat else 0:7.1f}ms  n={len(lat)}"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 检索质量与链路耗时评测（docs/11-§5）")
    ap.add_argument(
        "--mode",
        choices=("cloud", "hash"),
        default="cloud",
        help="cloud=真实云端 provider；hash=离线确定性基线",
    )
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条查询（冒烟用）")
    ap.add_argument("--json", type=str, default="", help="把结果写到该 JSON 文件")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    settings = make_settings(args.mode)
    queries = [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        queries = queries[: args.limit]

    provider = build_embedding_provider(settings)
    store = build_vector_store(settings)
    reranker = build_reranker(settings)

    print("=" * 80)
    print(f"RAG 评测  mode={args.mode}  top_k={args.top_k}  queries={len(queries)}")
    print(
        f"  embedding = {type(provider).__name__} / {settings.embedding_provider} / dim={settings.embedding_dim}"
    )
    print(
        f"  reranker  = {type(reranker).__name__} / enabled={settings.reranker_enabled} / top_n={settings.reranker_top_n}"
    )
    print(f"  store     = {type(store).__name__}   (INFRA_BACKEND={settings.infra_backend})")
    print(
        f"  chunk     = size={settings.chunk_size} overlap={settings.chunk_overlap} "
        f"merge_adjacent={settings.rag_merge_adjacent_chunks} threshold={settings.score_threshold}"
    )
    print("=" * 80)

    # ---- 入库 ----
    print("\n[1/2] 入库链路")
    t0 = time.perf_counter()
    rep = await ingest(settings, provider, store, verbose=args.verbose)
    wall = time.perf_counter() - t0
    if rep.truncated_docs:
        print(f"  ⚠️ 被 max_doc_chunks 截断：{rep.truncated_docs}")
    print(f"  文档 {rep.docs} 篇 → 切片 {rep.chunks} 块 / {rep.tokens} token")
    print(
        f"  parse={rep.parse_s:6.3f}s  split+merge={rep.chunk_s:6.3f}s  sha256={rep.hash_s:6.3f}s  "
        f"embed={rep.embed_s:6.3f}s  upsert={rep.upsert_s:6.3f}s"
    )
    print(f"  合计 {rep.total_s:.3f}s（墙钟 {wall:.3f}s）  向量化 {rep.embed_calls} 批")
    if rep.chunks:
        print(
            f"  单块均摊 {rep.total_s / rep.chunks * 1000:.1f} ms/块；"
            f"向量化占 {rep.embed_s / rep.total_s if rep.total_s else 0:.1%}"
        )

    # ---- 检索 ----
    print("\n[2/2] 检索链路  ARM A = 仅向量召回 / ARM B = 向量 + 重排")
    retriever = Retriever(
        settings=settings, embedding=provider, vector_store=store, reranker=reranker
    )
    arm_a = await run_arm(
        retriever, queries, settings=settings, with_rerank=False, top_k=args.top_k
    )
    arm_b = await run_arm(retriever, queries, settings=settings, with_rerank=True, top_k=args.top_k)

    scored = sum(1 for q in queries if q.get("gold_spans"))
    negatives = [q for q in queries if not q.get("gold_spans")]
    print_arm(arm_a, scored)
    print_arm(arm_b, scored)

    # ---- 重排增益 ----
    print("\n  重排增益（B - A，正数=重排更好）")
    gain = {}
    for k in (1, 3, 5):
        a = arm_a.hits[k] / scored if scored else 0.0
        b = arm_b.hits[k] / scored if scored else 0.0
        gain[k] = b - a
        print(f"    hit@{k}: {a:6.1%} → {b:6.1%}   Δ={(b - a) * 100:+5.1f}pp")
    print(f"    MRR  : {arm_a.mrr:.4f} → {arm_b.mrr:.4f}   Δ={arm_b.mrr - arm_a.mrr:+.4f}")
    print(
        f"    重排段近似增量（p50 之差）≈ {pct(arm_b.latencies, 0.50) - pct(arm_a.latencies, 0.50):.1f}ms"
    )

    # ---- 负例 ----
    if negatives:
        neg_ids = {q["id"] for q in negatives}
        tops = [
            r["top_score"] for r in arm_b.rows if r["id"] in neg_ids and r["top_score"] is not None
        ]
        if tops:
            print(
                f"\n  负例 {len(negatives)} 条（语料中确实无答案）：重排后最高分 "
                f"min={min(tops):.4f} max={max(tops):.4f} 均={statistics.fmean(tops):.4f}"
            )
        else:
            print(f"\n  负例 {len(negatives)} 条：全部返回空")

    # ---- §5.2 门槛 ----
    observed = {
        "recall@5": arm_b.hits[5] / scored if scored else 0.0,
        "recall@20": arm_a.hits[20] / scored if scored else 0.0,
        "MRR": arm_b.mrr,
        "rerank_gain": gain.get(5, 0.0),
    }
    print("\n  docs/11-§5.2 门槛")
    all_pass = True
    for name, threshold in GATES.items():
        value = observed[name]
        ok = value >= threshold
        all_pass &= ok
        print(f"    {'PASS' if ok else 'FAIL'}  {name:<12} {value:7.3f}  门槛 ≥ {threshold}")
    print(f"\n  结论：{'全部达标' if all_pass else '存在未达标项'}（mode={args.mode}）")

    # ---- 未命中诊断 ----
    neg_ids = {q["id"] for q in negatives}
    misses = [r for r in arm_b.rows if r["first_rank"] is None and r["id"] not in neg_ids]
    print(f"\n  重排臂未命中 {len(misses)} 条：")
    for r in misses[:12]:
        print(
            f"    {r['id']} [{r['type']}] returned={r['returned']} top_doc={r['top_doc']} «{r['query']}»"
        )

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "mode": args.mode,
                    "top_k": args.top_k,
                    "settings": {
                        "embedding_provider": settings.embedding_provider,
                        "embedding_model": settings.embedding_model,
                        "reranker_provider": settings.reranker_provider,
                        "reranker_model": settings.reranker_model,
                        "reranker_top_n": settings.reranker_top_n,
                        "chunk_size": settings.chunk_size,
                        "chunk_overlap": settings.chunk_overlap,
                    },
                    "ingest": {
                        "docs": rep.docs,
                        "chunks": rep.chunks,
                        "tokens": rep.tokens,
                        "parse_s": round(rep.parse_s, 4),
                        "chunk_s": round(rep.chunk_s, 4),
                        "hash_s": round(rep.hash_s, 4),
                        "embed_s": round(rep.embed_s, 4),
                        "upsert_s": round(rep.upsert_s, 4),
                        "total_s": round(rep.total_s, 4),
                        "embed_calls": rep.embed_calls,
                        "per_doc": rep.per_doc,
                    },
                    "arms": {
                        arm.name: {
                            "hits": {str(k): v for k, v in arm.hits.items()},
                            "scored": scored,
                            "mrr": round(arm.mrr, 4),
                            "doc_hits": arm.doc_hits,
                            "unanswered": arm.unanswered,
                            "latency_ms": {
                                "p50": round(pct(arm.latencies, 0.50), 2),
                                "p90": round(pct(arm.latencies, 0.90), 2),
                                "p99": round(pct(arm.latencies, 0.99), 2),
                                "mean": round(statistics.fmean(arm.latencies), 2)
                                if arm.latencies
                                else 0,
                            },
                            "rows": arm.rows,
                        }
                        for arm in (arm_a, arm_b)
                    },
                    "gates": {
                        k: {"value": round(v, 4), "threshold": GATES[k], "pass": v >= GATES[k]}
                        for k, v in observed.items()
                    },
                    "all_pass": all_pass,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n  结果已写入 {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
