"""单元测试：入库的两条**可观测事实** —— 截断可见（UP-01）与切片进度（UP-02）。

背景见 ``ai-platform-go/docs/10-大文件上传与索引优化清单.md`` §5.1。

这两条为什么必须单独守门，而不是「顺手加一条断言」：

* **静默截断**的后果是「接口回 202、正文丢 41%」，而所有既有断言
  （``status == INDEXED``、``chunk_count > 0``、KB 计数重算正确）**都会继续通过** ——
  这类缺陷不写专门的用例就永远测不出来。8MB 测试正文实测：切分产出 16,969 片、
  只入库 10,000 片，而只有一条 warning 落在日志里。
* **``progress`` 会在 embedding 阶段饱和**（``_EMBED_PROGRESS_START + span`` 封顶 95），
  此后只有 ``chunks_done`` 还在动。不钉住这个计数，「进度条停住」与「任务卡死」
  从响应上完全无法区分 —— 而这正是本次要修掉的体验问题。

断言方式刻意都用**确定值**（计数、布尔、精确字典），不用计时与百分比 ——
百分比依赖 ``EMBEDDING_BATCH_SIZE`` 与总片数的组合，改一个配置就会假红。
"""

from __future__ import annotations

import pytest
from tests.conftest import build_settings

from app.core.config import Settings
from app.core.exceptions import AppError
from app.infrastructure.storage.base import Document
from app.main import build_rag_services
from app.tasks.events import TaskEvent
from app.tasks.models import ResourceType, Task, TaskError, TaskType

#: 足以切出十几片以上的正文（每行约 17 token，``chunk_size`` 由用例决定）
_BODY = "".join(f"第 {index} 段用于验证入库事实的可观测性。\n" for index in range(200))

_USER = "u_ingest_facts"


async def _ingest(settings: Settings, *, body: str, doc_name: str) -> tuple[Document, Task]:
    """跑完一次完整入库，返回 ``(文档, 重新读取后的任务)``。

    ``task_runner="none"`` ⇒ 上传只建任务不自动执行，由测试显式调
    ``_ingest_document``。这与 ``test_rag_ingestion_threads.py`` 同一套路子：
    把「接口路径」与「Worker 路径」分开测，避免用例通过后台协程不确定地串行。
    """
    services = build_rag_services(settings)
    kb = await services["kb_service"].create(
        user_id=_USER,
        payload={"name": f"kb-{doc_name}", "chunk_size": 128, "chunk_overlap": 16},
    )
    uploaded = await services["document_service"].upload(
        kb_id=kb.id, user_id=_USER, text=body, doc_name=doc_name
    )
    task = await services["tasks"].get(uploaded.task_id)
    await services["ingestion"]._ingest_document(task)
    document = await services["repos"].documents.get(uploaded.doc_id, _USER)
    # 任务状态在 store 里是快照，必须重新读；直接看旧对象只会拿到上传时的初值
    return document, await services["tasks"].get(uploaded.task_id)


# ---------------------------------------------------------------------------
# UP-01 · 截断可见
# ---------------------------------------------------------------------------
async def test_truncated_document_reports_produced_and_kept_counts() -> None:
    """被 ``MAX_DOC_CHUNKS`` 截断时，调用方能同时看到「本该多少」与「实际多少」。"""
    document, task = await _ingest(
        build_settings(task_runner="none", max_doc_chunks=3),
        body=_BODY,
        doc_name="truncated.txt",
    )

    assert document.truncated is True, "丢了正文却不说，正是 UP-01 要消灭的假成功"
    assert document.chunks_total is not None
    assert document.chunks_total > 3, "chunks_total 是**截断前**的产出数，不是截断后的"
    assert document.chunk_count == 3, "chunk_count 是实际入库数，必须等于上限"
    assert document.chunks_total > document.chunk_count

    # ``to_dict`` 是 ``GET /documents/{id}`` 的唯一数据来源（DocumentOut.model_validate），
    # 所以钉住它才算钉住接口契约
    payload = document.to_dict()
    assert payload["truncated"] is True
    assert payload["chunks_total"] == document.chunks_total
    assert payload["chunk_count"] == 3

    # 任务侧的总数是**待向量化**的数量（= 实际入库数），与文档的产出数是两个量：
    # 进度条的分母必须是真实工作量，否则 ETA 永远算不准
    assert task.chunks_total == 3
    assert task.chunks_done == 3


async def test_untruncated_document_reports_consistent_counts() -> None:
    """没被截断时三个量自洽（``chunks_total == chunk_count``、``truncated=False``）。

    这条是上一条的**反向对照**：只断言「截断时 truncated=True」的用例，
    在实现把 ``truncated`` 恒置为 ``True`` 时也会通过。
    """
    document, task = await _ingest(
        build_settings(task_runner="none", max_doc_chunks=10000),
        body=_BODY,
        doc_name="intact.txt",
    )

    assert document.truncated is False
    assert document.chunks_total is not None
    assert document.chunks_total == document.chunk_count > 0
    assert task.chunks_total == task.chunks_done == document.chunk_count


async def test_failed_document_keeps_no_fake_truncation_flag() -> None:
    """失败路径不得留下「截断」这种自相矛盾的事实。

    刻意走 ``handle``（而不是像另外几条那样直接调 ``_ingest_document``）：
    写回 ``status=FAILED`` 与 ``error_code`` 的是 ``handle`` 的 ``except`` 分支，
    只调内层函数的话「失败路径到底落了什么」根本没被执行到。
    """
    settings = build_settings(task_runner="none", max_doc_chunks=10000)
    services = build_rag_services(settings)
    kb = await services["kb_service"].create(user_id=_USER, payload={"name": "kb-fail"})
    uploaded = await services["document_service"].upload(
        kb_id=kb.id, user_id=_USER, text="太短", doc_name="tiny.txt"
    )
    task = await services["tasks"].get(uploaded.task_id)
    # ``track`` 只允许 QUEUED → RUNNING，所以要先手工置 QUEUED
    await services["tasks"].mark_queued(task.id)

    # ``min_doc_chars`` 之下会被判为不可处理（空/过短文档），走的是失败路径
    with pytest.raises(AppError):
        await services["ingestion"].handle(task)

    document = await services["repos"].documents.get(uploaded.doc_id, _USER)
    assert document.status == "FAILED"
    assert document.truncated is False, "失败不是截断，不该借用截断标记"
    assert document.chunks_total is None, "还没走到切分 ⇒ 是「未知」而不是「0 片」"


# ---------------------------------------------------------------------------
# UP-02 · 切片进度的三条不变量
# ---------------------------------------------------------------------------
async def test_report_progress_keeps_chunk_counters_monotonic() -> None:
    """``chunks_done`` 单调不减、``chunks_total`` 定下后不得变更。"""
    services = build_rag_services(build_settings(task_runner="none"))
    task, _ = await services["tasks"].create(
        type_=TaskType.DOCUMENT_INGEST,
        user_id=_USER,
        resource_type=ResourceType.DOCUMENT,
        resource_id="doc_progress",
    )
    report = services["tasks"].report_progress

    first = await report(task.id, stage="CHUNKING", progress=35, chunks_total=100, chunks_done=0)
    assert (first.chunks_total, first.chunks_done) == (100, 0)

    advanced = await report(
        task.id, stage="EMBEDDING", progress=55, chunks_total=100, chunks_done=10
    )
    assert advanced.chunks_done == 10

    # 总数漂移会让客户端算出的 ETA 在批与批之间突然跳变，而原因从响应里看不出来
    with pytest.raises(AppError):
        await report(task.id, stage="EMBEDDING", progress=60, chunks_total=99)

    # 回退与 progress 回退同源：会掩盖「阶段乱序执行」这类真问题
    with pytest.raises(AppError):
        await report(task.id, stage="EMBEDDING", progress=60, chunks_done=5)


async def test_retry_resets_chunk_counters() -> None:
    """重试必须把计数一并归零，否则新一次切分的总数会被「不得变更」拦住。"""
    services = build_rag_services(build_settings(task_runner="none"))
    task, _ = await services["tasks"].create(
        type_=TaskType.DOCUMENT_INGEST,
        user_id=_USER,
        resource_type=ResourceType.DOCUMENT,
        resource_id="doc_retry",
    )
    await services["tasks"].report_progress(
        task.id, stage="EMBEDDING", progress=55, chunks_total=100, chunks_done=10
    )
    await services["tasks"].fail(task.id, TaskError(code="EMBEDDING_FAILED"))

    retried, requeued = await services["tasks"].retry(task.id, _USER)
    assert requeued is True
    assert (retried.chunks_total, retried.chunks_done) == (0, 0)

    # 归零之后，新一轮可以重新定下总数（不归零的话这里会抛「切片总数不得变更」）
    again = await services["tasks"].report_progress(
        task.id, stage="CHUNKING", progress=35, chunks_total=42
    )
    assert again.chunks_total == 42


# ---------------------------------------------------------------------------
# UP-02 · 事件契约是**追加式**的
# ---------------------------------------------------------------------------
def test_progress_event_shape_is_additive() -> None:
    """不进切片计数的旧调用产出**逐字节不变**的帧。

    这不是洁癖：``GET /tasks/{id}/events`` 的 ``data`` 是公开契约，
    旧客户端按 ``{stage, progress}`` 取值。若把计数无条件下发，
    摘要/记忆抽取这类任务的进度帧会凭空多出 ``chunks_total: 0`` ——
    把「没有切片概念」渲染成「一片都没有」，也让既有断言全红。
    """
    assert TaskEvent.progress(stage="CHUNKING", progress=35).data == {
        "stage": "CHUNKING",
        "progress": 35,
    }
    assert TaskEvent.progress(
        stage="EMBEDDING", progress=55, chunks_done=30, chunks_total=100
    ).data == {
        "stage": "EMBEDDING",
        "progress": 55,
        "chunks_total": 100,
        "chunks_done": 30,
    }
