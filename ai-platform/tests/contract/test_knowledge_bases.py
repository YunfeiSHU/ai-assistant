"""知识库接口契约测试（``docs/06`` §2、``AC-RAG-01/02``）。

覆盖点：

* 生命周期（建 / 列 / 查 / 改 / 删）与多租户隔离（``REQ-RAG-011``）；
* 参数校验（``CHUNK_STRATEGY_INVALID``）与配置继承（KB 覆盖全局默认）；
* 删除语义：非空必须 ``force=true``，删除后向量与切片计数归零（``REQ-RAG-010``）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.support.rag import create_kb, ingest_text, search

from app.main import create_app

PREFIX = "/api/v1"


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def test_create_knowledge_base_returns_defaults(rag_client: TestClient) -> None:
    """新建 KB 时未传的字段取全局默认，且计数为 0（``AC-RAG-01``）。"""
    kb = create_kb(rag_client, name="售后政策", description="客服问答语料")

    assert kb["id"].startswith("kb_")
    assert kb["name"] == "售后政策"
    assert kb["description"] == "客服问答语料"
    assert kb["status"] == "active"
    assert kb["document_count"] == 0
    assert kb["chunk_count"] == 0
    # 默认值必须落库而不是「响应里补一个」——否则后续入库会拿到 None
    assert kb["chunk_size"] == 512
    assert kb["chunk_overlap"] == 64
    assert kb["embedding_dim"] > 0
    assert kb["created_at"]


def test_create_knowledge_base_rejects_duplicate_name(rag_client: TestClient) -> None:
    """同名 → ``409 KB_NAME_CONFLICT``（名称是给用户看的，必须唯一）。"""
    create_kb(rag_client, name="重复名")

    response = rag_client.post(f"{PREFIX}/knowledge-bases", json={"name": "重复名"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KB_NAME_CONFLICT"


def test_create_knowledge_base_rejects_invalid_chunk_strategy(rag_client: TestClient) -> None:
    """``chunk_overlap >= chunk_size`` → ``400 CHUNK_STRATEGY_INVALID``。"""
    response = rag_client.post(
        f"{PREFIX}/knowledge-bases",
        json={"name": "参数错误", "chunk_size": 256, "chunk_overlap": 256},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CHUNK_STRATEGY_INVALID"


@pytest.mark.parametrize("chunk_size", [64, 4096])
def test_create_knowledge_base_rejects_out_of_range_chunk_size(
    rag_client: TestClient, chunk_size: int
) -> None:
    """``chunk_size`` 超出 ``[128, 2048]`` → 400。"""
    response = rag_client.post(
        f"{PREFIX}/knowledge-bases",
        json={"name": f"越界-{chunk_size}", "chunk_size": chunk_size},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CHUNK_STRATEGY_INVALID"


def test_list_knowledge_bases_is_paginated(rag_client: TestClient) -> None:
    """分页按创建时间倒序，两页之间不重不漏。"""
    for index in range(3):
        create_kb(rag_client, name=f"分页-{index}")

    response = rag_client.get(f"{PREFIX}/knowledge-bases", params={"limit": 2})
    body = response.json()

    assert response.status_code == 200
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["next_cursor"]

    page2 = rag_client.get(
        f"{PREFIX}/knowledge-bases", params={"limit": 2, "cursor": body["next_cursor"]}
    ).json()
    ids = {item["id"] for item in body["items"]} | {item["id"] for item in page2["items"]}
    assert len(ids) == 3, "两页之间不应出现重复或遗漏"


def test_get_knowledge_base_of_other_user_returns_404(
    rag_client: TestClient, other_user_headers: dict[str, str]
) -> None:
    """跨用户访问返回 ``404`` 而不是 ``403``（不泄露「该 ID 存在」）。"""
    kb = create_kb(rag_client)

    response = rag_client.get(f"{PREFIX}/knowledge-bases/{kb['id']}", headers=other_user_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KB_NOT_FOUND"


def test_list_knowledge_bases_is_isolated(
    rag_client: TestClient, other_user_headers: dict[str, str]
) -> None:
    """另一个用户看不到、也搜不到我的 KB（``REQ-RAG-011``）。"""
    kb = create_kb(rag_client, name="私有库")
    ingest_text(rag_client, kb["id"], "仅本租户可见的内容。" * 10)

    listing = rag_client.get(f"{PREFIX}/knowledge-bases", headers=other_user_headers)
    assert listing.json()["items"] == []

    hidden = rag_client.post(
        f"{PREFIX}/knowledge-bases/{kb['id']}/search",
        json={"query": "本租户可见"},
        headers=other_user_headers,
    )
    assert hidden.status_code == 404


def test_update_knowledge_base_is_partial(rag_client: TestClient) -> None:
    """PATCH 只改传入字段（``exclude_unset``），未传字段保持原值。"""
    kb = create_kb(
        rag_client, name="原名", description="原描述", chunk_size=1000, chunk_overlap=100
    )

    response = rag_client.patch(
        f"{PREFIX}/knowledge-bases/{kb['id']}", json={"description": "新描述"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "新描述"
    assert body["name"] == "原名"
    assert body["chunk_size"] == 1000


def test_update_knowledge_base_rejects_empty_patch(rag_client: TestClient) -> None:
    """空 PATCH 是无效请求：静默返回「没变」会让人以为保存成功了。"""
    kb = create_kb(rag_client)

    response = rag_client.patch(f"{PREFIX}/knowledge-bases/{kb['id']}", json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_delete_knowledge_base_requires_force_when_not_empty(rag_client: TestClient) -> None:
    """非空 KB 直接删 → ``409 KB_NOT_EMPTY``；``force=true`` 才级联清理。"""
    kb = create_kb(rag_client)
    ingest_text(rag_client, kb["id"], "退款时效为 7 个工作日。" * 10)

    blocked = rag_client.delete(f"{PREFIX}/knowledge-bases/{kb['id']}")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "KB_NOT_EMPTY"

    forced = rag_client.delete(f"{PREFIX}/knowledge-bases/{kb['id']}", params={"force": True})
    assert forced.status_code == 200
    body = forced.json()
    assert body["documents"] == 1
    assert body["vectors"] > 0
    assert body["objects"] == 1

    assert rag_client.get(f"{PREFIX}/knowledge-bases/{kb['id']}").status_code == 404


def test_delete_empty_knowledge_base_is_idempotent(rag_client: TestClient) -> None:
    """空 KB 无需 ``force`` 即可删除；再删一次仍是 404（不是 500）。"""
    kb = create_kb(rag_client)

    first = rag_client.delete(f"{PREFIX}/knowledge-bases/{kb['id']}")
    second = rag_client.delete(f"{PREFIX}/knowledge-bases/{kb['id']}")

    assert first.status_code == 200
    assert first.json()["documents"] == 0
    assert second.status_code == 404


def test_knowledge_base_limit(
    rag_app: tuple[Any, Any],
    make_token: Any,
) -> None:
    """超过 ``max_kb_count`` → ``429 KB_LIMIT_EXCEEDED``。"""
    _, base = rag_app
    settings = base.model_copy(update={"max_kb_count": 1})
    headers = {"Authorization": f"Bearer {make_token('u_kb_limit', settings)}"}
    with TestClient(create_app(settings), headers=headers) as limited:
        create_kb(limited, name="唯一")
        response = limited.post(f"{PREFIX}/knowledge-bases", json={"name": "超出"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KB_LIMIT_EXCEEDED"


# ---------------------------------------------------------------------------
# 检索接口
# ---------------------------------------------------------------------------


def test_search_returns_matching_chunk(rag_client: TestClient) -> None:
    """上传后检索命中文档（``AC-RAG-05``）。"""
    kb = create_kb(rag_client)
    ingest_text(
        rag_client,
        kb["id"],
        "退款申请需在收到货物后 7 个工作日内提交，逾期不予受理。" * 5,
        doc_name="售后政策.md",
    )

    query = "退款 7 个工作日 逾期"
    body = search(rag_client, kb["id"], query)

    # 响应字段与 ``docs/06`` §5.1 逐字对齐
    assert body["query"] == query
    assert body["recalled"] >= 1
    assert body["returned"] == len(body["items"])
    assert body["elapsed_ms"] >= 0
    assert body["items"], "刚入库的文档应当能被检索到"
    top = body["items"][0]
    assert top["index"] == 1
    assert top["doc_name"] == "售后政策.md"
    assert top["chunk_id"].startswith("chk_")
    assert top["doc_id"]
    assert top["content"]


def test_search_on_empty_kb_returns_no_items(rag_client: TestClient) -> None:
    """空库检索是正常的空结果，不是错误（`REQ-RAG-008`）。"""
    kb = create_kb(rag_client)

    body = search(rag_client, kb["id"], "任意查询")

    assert body["items"] == []
    assert body["recalled"] == 0


def test_search_rejects_top_k_out_of_range(rag_client: TestClient) -> None:
    """``top_k`` 越界由 schema 拦下（400），不进业务逻辑。"""
    kb = create_kb(rag_client)

    response = rag_client.post(
        f"{PREFIX}/knowledge-bases/{kb['id']}/search", json={"query": "x", "top_k": 0}
    )

    assert response.status_code == 400


def test_search_without_rerank_reports_null_rerank_score(rag_client: TestClient) -> None:
    """``with_rerank=false`` 时 ``rerank_used=false`` 且 ``rerank_score`` 为 null（``AC-RAG-12``）。"""
    kb = create_kb(rag_client)
    ingest_text(rag_client, kb["id"], "发票抬头修改需要提交工单。" * 10)

    body = search(rag_client, kb["id"], "发票抬头", with_rerank=False)

    assert body["rerank_used"] is False
    for item in body["items"]:
        assert item["rerank_score"] is None, "没重排就不该给出重排分"
        assert item["score"] == pytest.approx(item["vector_score"])
