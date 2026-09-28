"""文档入库契约测试（``docs/06`` §3，``AC-RAG-02/03/04/06/07/15/16``）。

这套用例的价值在于**验证异步流水线的可观测结果**：接口只返回 ``202``，
真正的解析/切分/向量化结果必须能从 ``GET /documents/{id}``、
``GET /documents/{id}/chunks`` 与 ``POST /search`` 三处看到。
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Any

from fastapi.testclient import TestClient
from tests.support.rag import (
    create_kb,
    get_task,
    ingest_text,
    search,
    upload_file,
    upload_text,
    wait_task,
    wait_task_success,
)

PREFIX = "/api/v1"

MARKDOWN = """# 售后政策

## 退款

### 时效

自签收之日起 7 个自然日内可申请退款，逾期不予受理。
审核通过后 3 个工作日内原路退回。

### 条件

商品需保持完好，附件齐全。

## 换货

换货不受 7 天限制，但需提供质量问题凭证。
"""


def _blank_pdf() -> bytes:
    """生成一个「有页面但无文本层」的合法 PDF（模拟扫描件，``AC-RAG-16``）。

    用 ``pypdf`` 现造而不是手工拼字节：手工拼的 PDF 缺 xref，不同版本的解析器
    行为不一致，用例会变成「取决于库版本」。造出来的这个一定是结构合法、
    只是没有文字——正是扫描件的特征。
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 上传接口本身（同步部分）
# ---------------------------------------------------------------------------


def test_upload_text_returns_202_with_task(rag_client: TestClient) -> None:
    """上传只做校验 + 建任务，返回 ``202`` 与 ``task_id``（``AC-RAG-02``）。"""
    kb = create_kb(rag_client)

    accepted = upload_text(rag_client, kb["id"], "售后政策：7 天无理由退货。" * 5, doc_name="政策")

    assert accepted["doc_id"].startswith("doc_")
    assert accepted["task_id"].startswith("task_")
    assert accepted["status"] in {"PENDING", "QUEUED", "RUNNING"}
    assert accepted["duplicated"] is False
    assert len(accepted["content_sha256"]) == 64
    # 纯文本上传时显示名缺扩展名会自动补 ``.txt``，否则下游解析器无从分派
    assert accepted["doc_name"] == "政策.txt"

    task = get_task(rag_client, accepted["task_id"])
    assert task["type"] == "document_ingest"
    assert task["resource_id"] == accepted["doc_id"]


def test_upload_requires_exactly_one_of_file_or_text(rag_client: TestClient) -> None:
    """``file`` 与 ``text`` 都不给（或都给）→ ``400``，不允许静默挑一个。"""
    kb = create_kb(rag_client)

    neither = rag_client.post(f"{PREFIX}/knowledge-bases/{kb['id']}/documents", data={})
    both = rag_client.post(
        f"{PREFIX}/knowledge-bases/{kb['id']}/documents",
        data={"text": "hello"},
        files={"file": ("a.txt", b"hello", "text/plain")},
    )

    assert neither.status_code == 400
    assert both.status_code == 400
    assert neither.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_upload_rejects_unsupported_extension(rag_client: TestClient) -> None:
    """扩展名不支持 → ``415``，而且**在接口层**返回（不落成一个失败的入库任务）。"""
    kb = create_kb(rag_client)

    response = rag_client.post(
        f"{PREFIX}/knowledge-bases/{kb['id']}/documents",
        files={"file": ("data.xlsx", b"PK\x03\x04not-a-docx", "application/octet-stream")},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_upload_rejects_content_mismatching_extension(rag_client: TestClient) -> None:
    """魔数与扩展名不一致 → ``415``（``docs/06`` §3.2 要求两者都校验）。"""
    kb = create_kb(rag_client)

    response = rag_client.post(
        f"{PREFIX}/knowledge-bases/{kb['id']}/documents",
        files={"file": ("fake.md", b"%PDF-1.4 not markdown", "text/markdown")},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_upload_rejects_bad_metadata_json(rag_client: TestClient) -> None:
    """``metadata`` 不是合法 JSON → ``400``（而不是被忽略）。"""
    kb = create_kb(rag_client)

    response = rag_client.post(
        f"{PREFIX}/knowledge-bases/{kb['id']}/documents",
        data={"text": "内容", "metadata": "{不是 json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_upload_enforces_file_size_limit(
    make_rag_client: Callable[..., tuple[Any, Any, TestClient]],
) -> None:
    """超出 ``upload_max_mb`` → ``413 FILE_TOO_LARGE``。"""
    _, _, rag_client = make_rag_client(upload_max_mb=0)
    with rag_client:
        kb = create_kb(rag_client)
        response = rag_client.post(
            f"{PREFIX}/knowledge-bases/{kb['id']}/documents",
            files={"file": ("a.txt", b"x", "text/plain")},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_upload_enforces_document_limit(
    make_rag_client: Callable[..., tuple[Any, Any, TestClient]],
) -> None:
    """单 KB 文档数超限 → ``409 KB_DOCUMENT_LIMIT_EXCEEDED``。"""
    _, _, rag_client = make_rag_client(max_kb_documents=1)
    with rag_client:
        kb = create_kb(rag_client)
        upload_text(rag_client, kb["id"], "第一个文档。" * 10)
        response = rag_client.post(
            f"{PREFIX}/knowledge-bases/{kb['id']}/documents",
            data={"text": "第二个文档。" * 10},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KB_DOCUMENT_LIMIT_EXCEEDED"


# ---------------------------------------------------------------------------
# 流水线结果（异步部分）
# ---------------------------------------------------------------------------


def test_ingest_pipeline_produces_chunks_and_vectors(rag_client: TestClient) -> None:
    """解析 → 切分 → 向量化 → 可检索，且 KB 计数被重算（``AC-RAG-03``）。"""
    kb = create_kb(rag_client, chunk_size=256, chunk_overlap=32)

    accepted, document = ingest_text(rag_client, kb["id"], MARKDOWN, doc_name="政策.md")

    assert document["status"] == "INDEXED"
    assert document["chunk_count"] > 0
    assert document["char_count"] > 0
    assert document["indexed_at"]
    assert document["error"] is None

    # 计数是按实际数据重算的，不是 +1
    detail = rag_client.get(f"{PREFIX}/knowledge-bases/{kb['id']}").json()
    assert detail["document_count"] == 1
    assert detail["chunk_count"] == document["chunk_count"]

    hits = search(rag_client, kb["id"], "退款 时效 7 个自然日")
    assert hits["items"], "入库完成后必须能被检索到"
    assert hits["items"][0]["doc_id"] == accepted["doc_id"]


def test_markdown_headings_become_heading_path(rag_client: TestClient) -> None:
    """Markdown 三级标题写入 ``heading_path``（``AC-RAG-06``）。"""
    kb = create_kb(rag_client)
    _, document = ingest_text(rag_client, kb["id"], MARKDOWN, doc_name="政策.md")

    chunks = rag_client.get(
        f"{PREFIX}/documents/{document['id']}/chunks", params={"limit": 50}
    ).json()

    assert chunks["items"]
    paths = [item["heading_path"] for item in chunks["items"] if item["heading_path"]]
    assert paths, "带标题的 Markdown 必须产出非空 heading_path"
    assert any("退款" in path and "时效" in path for path in paths), paths


def test_chunk_size_bounds_are_respected(rag_client: TestClient) -> None:
    """切分结果长度受 ``chunk_size`` 约束（``AC-RAG-07``）。"""
    kb = create_kb(rag_client, chunk_size=128, chunk_overlap=16)
    _, document = ingest_text(rag_client, kb["id"], MARKDOWN * 4, doc_name="政策.md")

    chunks = rag_client.get(
        f"{PREFIX}/documents/{document['id']}/chunks", params={"limit": 100}
    ).json()

    assert chunks["items"]
    from app.core.tokens import count_tokens

    for item in chunks["items"]:
        assert count_tokens(item["content"]) <= 128 * 1.5, item["content"][:60]
    # 相邻切片的字符区间必须连续覆盖原文，否则「引用溯源」指向的位置是错的
    assert chunks["items"][0]["char_start"] == 0
    for previous, current in zip(chunks["items"], chunks["items"][1:], strict=False):
        assert current["char_start"] >= previous["char_start"]


def test_chunk_params_frozen_per_document(rag_client: TestClient) -> None:
    """切片元数据固化本次切分参数，改 KB 配置**不回溯**已有文档（``REQ-RAG-002``）。"""
    kb = create_kb(rag_client, chunk_size=256, chunk_overlap=32)
    _, document = ingest_text(rag_client, kb["id"], MARKDOWN, doc_name="政策.md")

    rag_client.patch(f"{PREFIX}/knowledge-bases/{kb['id']}", json={"chunk_size": 1024})

    chunks = rag_client.get(f"{PREFIX}/documents/{document['id']}/chunks").json()
    for item in chunks["items"]:
        assert item["metadata"]["chunk_size"] == 256, "老切片必须记得自己是怎么切出来的"


def test_no_runner_means_no_vectors_yet(
    make_rag_client: Callable[..., tuple[Any, Any, TestClient]],
) -> None:
    """``task_runner=none``：任务已建但没执行 → 文档仍待处理且检索为空（``AC-RAG-04``）。

    这是「接口 P95 ≤ 200ms」的结构性证据：``202`` 之后一切都在任务里，
    不上天入地地等 Worker，文档状态与向量数量就是「还没开始」的样子。
    """
    _, _, rag_client = make_rag_client(task_runner="none")
    with rag_client:
        kb = create_kb(rag_client)
        accepted = upload_text(rag_client, kb["id"], "这段内容暂时不会被向量化。" * 10)

        task = get_task(rag_client, accepted["task_id"])
        assert task["status"] in {"PENDING", "QUEUED"}
        assert task["progress"] == 0

        document = rag_client.get(f"{PREFIX}/documents/{accepted['doc_id']}").json()
        assert document["status"] == "PENDING"
        assert document["chunk_count"] == 0

        empty = search(rag_client, kb["id"], "向量化")
        assert empty["items"] == []
        assert empty["recalled"] == 0


def test_empty_parsed_text_fails_with_unprocessable(rag_client: TestClient) -> None:
    """扫描版 PDF（无文本层）→ 任务 FAILED + ``UNPROCESSABLE_DOCUMENT``（``AC-RAG-16``）。"""
    kb = create_kb(rag_client)

    accepted = upload_file(
        rag_client, kb["id"], _blank_pdf(), filename="scan.pdf", content_type="application/pdf"
    )
    task = wait_task(rag_client, accepted["task_id"])

    assert task["status"] == "FAILED"
    assert task["error"]["code"] == "UNPROCESSABLE_DOCUMENT"

    document = rag_client.get(f"{PREFIX}/documents/{accepted['doc_id']}").json()
    assert document["status"] == "FAILED"
    assert document["error"]["code"] == "UNPROCESSABLE_DOCUMENT"


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------


def test_duplicate_upload_is_rejected_with_existing_doc_id(rag_client: TestClient) -> None:
    """同内容已入库 → ``409 DOCUMENT_DUPLICATE`` 并带上已有 ``doc_id``（``REQ-RAG-007``）。"""
    kb = create_kb(rag_client)
    body = "完全一样的内容，第二次上传应当被拦下。" * 10
    first, _ = ingest_text(rag_client, kb["id"], body, doc_name="原件.md")

    response = rag_client.post(
        f"{PREFIX}/knowledge-bases/{kb['id']}/documents",
        data={"text": body, "doc_name": "副本.md"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "DOCUMENT_DUPLICATE"
    assert error["details"]["doc_id"] == first["doc_id"]


def test_duplicate_inflight_upload_is_idempotent(
    make_rag_client: Callable[..., tuple[Any, Any, TestClient]],
) -> None:
    """仍在处理中的同内容文档 → 幂等返回同一 ``doc_id``（不报错、不重复建任务）。"""
    _, _, rag_client = make_rag_client(task_runner="none")
    with rag_client:
        kb = create_kb(rag_client)
        body = "内容相同的两次上传，第二次应当拿到同一个 doc_id。" * 10
        first = upload_text(rag_client, kb["id"], body, doc_name="第一次.md")
        second = upload_text(rag_client, kb["id"], body, doc_name="第二次.md")

        assert second["duplicated"] is True
        assert second["doc_id"] == first["doc_id"]
        assert second["task_id"] == first["task_id"]


def test_failed_document_can_be_reuploaded(rag_client: TestClient) -> None:
    """失败文档允许重传：复用同一行并重新投递（不能因为内容哈希相同就永久堵死）。"""
    kb = create_kb(rag_client)
    accepted = upload_file(
        rag_client, kb["id"], _blank_pdf(), filename="scan.pdf", content_type="application/pdf"
    )
    wait_task(rag_client, accepted["task_id"])

    again = upload_file(
        rag_client, kb["id"], _blank_pdf(), filename="scan.pdf", content_type="application/pdf"
    )

    assert again["doc_id"] == accepted["doc_id"], "复用同一行（避免同一 KB 内出现两个同哈希文档）"
    assert again["duplicated"] is False
    task = wait_task(rag_client, again["task_id"])
    assert task["status"] == "FAILED", "内容没变，重传仍然应当失败（而不是假装成功）"


# ---------------------------------------------------------------------------
# 列表 / 详情 / 删除
# ---------------------------------------------------------------------------


def test_list_documents_filters_by_status(rag_client: TestClient) -> None:
    """``status`` 过滤与分页。"""
    kb = create_kb(rag_client)
    ingest_text(rag_client, kb["id"], "已入库的文档。" * 10, doc_name="ok.md")
    upload_file(
        rag_client, kb["id"], _blank_pdf(), filename="scan.pdf", content_type="application/pdf"
    )

    indexed = rag_client.get(
        f"{PREFIX}/knowledge-bases/{kb['id']}/documents", params={"status": "INDEXED"}
    ).json()
    assert [item["doc_name"] for item in indexed["items"]] == ["ok.md"]

    everything = rag_client.get(f"{PREFIX}/knowledge-bases/{kb['id']}/documents").json()
    assert len(everything["items"]) == 2


def test_document_detail_is_tenant_scoped(
    rag_client: TestClient, other_user_headers: dict[str, str]
) -> None:
    """跨用户读文档 → ``404``。"""
    kb = create_kb(rag_client)
    accepted, _ = ingest_text(rag_client, kb["id"], "私有文档。" * 10)

    response = rag_client.get(
        f"{PREFIX}/documents/{accepted['doc_id']}", headers=other_user_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_document_not_found(rag_client: TestClient) -> None:
    """不存在的文档 → ``404 DOCUMENT_NOT_FOUND``。"""
    response = rag_client.get(f"{PREFIX}/documents/doc_01M3KGRC3HQ38BN4HEPYRCK73Q")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_delete_document_cascades_all_stores(rag_client: TestClient) -> None:
    """删除文档：向量、切片、对象三处都清干净，且检索不再命中（``AC-RAG-15``）。"""
    kb = create_kb(rag_client)
    accepted, document = ingest_text(rag_client, kb["id"], MARKDOWN, doc_name="政策.md")
    assert search(rag_client, kb["id"], "退款 时效")["items"]

    deleted = rag_client.delete(f"{PREFIX}/documents/{accepted['doc_id']}")
    assert deleted.status_code == 202
    assert deleted.json()["task_id"]
    wait_task_success(rag_client, deleted.json()["task_id"])

    assert rag_client.get(f"{PREFIX}/documents/{accepted['doc_id']}").status_code == 404
    assert rag_client.get(f"{PREFIX}/documents/{accepted['doc_id']}/chunks").status_code == 404
    assert search(rag_client, kb["id"], "退款 时效")["items"] == []

    detail = rag_client.get(f"{PREFIX}/knowledge-bases/{kb['id']}").json()
    assert detail["document_count"] == 0
    assert detail["chunk_count"] == 0
    assert document["chunk_count"] > 0, "删之前确实是有切片的"


def test_delete_document_is_idempotent_after_completion(rag_client: TestClient) -> None:
    """已删除的文档再删一次 → ``404``（不是 500，也不产生第二个任务）。"""
    kb = create_kb(rag_client)
    accepted, _ = ingest_text(rag_client, kb["id"], "待删除文档。" * 10)
    first = rag_client.delete(f"{PREFIX}/documents/{accepted['doc_id']}")
    wait_task_success(rag_client, first.json()["task_id"])

    second = rag_client.delete(f"{PREFIX}/documents/{accepted['doc_id']}")

    assert second.status_code == 404


def test_chunks_pagination(rag_client: TestClient) -> None:
    """切片列表按 ``chunk_index`` 正序分页，游标可用。"""
    kb = create_kb(rag_client, chunk_size=128, chunk_overlap=16)
    _, document = ingest_text(rag_client, kb["id"], MARKDOWN * 3, doc_name="政策.md")

    page1 = rag_client.get(
        f"{PREFIX}/documents/{document['id']}/chunks", params={"limit": 2}
    ).json()
    assert len(page1["items"]) == 2
    assert [item["chunk_index"] for item in page1["items"]] == [0, 1]
    assert page1["has_more"] is True

    page2 = rag_client.get(
        f"{PREFIX}/documents/{document['id']}/chunks",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    ).json()
    assert page2["items"][0]["chunk_index"] == 2
