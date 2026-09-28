"""任务接口契约测试（``docs/08`` §4，``AC-TASK-01..09``）。

任务层最容易被写成「看起来对但状态会串」：重复取消、重试超限、终态再操作。
这里逐条把状态机的**对外可观察行为**钉住。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from io import BytesIO
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.support.rag import create_kb, get_task, upload_file, upload_text, wait_task

PREFIX = "/api/v1"


def _blank_pdf() -> bytes:
    """结构合法但无文本层的 PDF → 入库必然失败（用来制造 FAILED 任务）。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _failing_task(rag_client: TestClient, kb_id: str) -> dict[str, Any]:
    """上传一个必然失败（扫描件）的文档并等任务结束。"""
    accepted = upload_file(
        rag_client, kb_id, _blank_pdf(), filename="scan.pdf", content_type="application/pdf"
    )
    return wait_task(rag_client, accepted["task_id"])


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def test_task_shape_matches_contract(rag_client: TestClient) -> None:
    """成功任务的字段与 ``docs/08`` §3 一致，且终态给出可操作标志。"""
    kb = create_kb(rag_client)
    accepted = upload_text(rag_client, kb["id"], "退款政策：7 个自然日。" * 10, doc_name="政策.md")
    task = wait_task(rag_client, accepted["task_id"])

    assert set(task) == {
        "id",
        "type",
        "status",
        "resource_type",
        "resource_id",
        "progress",
        "stage",
        "retry_count",
        "max_retries",
        "error",
        "cancelable",
        "retryable",
        "created_at",
        "queued_at",
        "started_at",
        "finished_at",
        "updated_at",
    }
    assert task["status"] == "SUCCEEDED"
    assert task["type"] == "document_ingest"
    assert task["resource_type"] == "document"
    assert task["progress"] == 100
    assert task["finished_at"]
    assert task["error"] is None
    # 终态不可再操作 —— 前端不该靠猜
    assert task["cancelable"] is False
    assert task["retryable"] is False


def test_task_list_filters(rag_client: TestClient) -> None:
    """按 ``status`` / ``type`` / ``resource_id`` 过滤。"""
    kb = create_kb(rag_client)
    accepted = upload_text(rag_client, kb["id"], "会被检索的文档。" * 10)
    wait_task(rag_client, accepted["task_id"])

    by_type = rag_client.get(f"{PREFIX}/tasks", params={"type": "document_ingest"}).json()
    assert [item["id"] for item in by_type["items"]] == [accepted["task_id"]]

    by_resource = rag_client.get(
        f"{PREFIX}/tasks", params={"resource_id": accepted["doc_id"]}
    ).json()
    assert len(by_resource["items"]) == 1

    by_status = rag_client.get(f"{PREFIX}/tasks", params={"status": "SUCCEEDED"}).json()
    assert accepted["task_id"] in [item["id"] for item in by_status["items"]]

    empty = rag_client.get(f"{PREFIX}/tasks", params={"status": "CANCELED"}).json()
    assert empty["items"] == []
    assert empty["has_more"] is False

    everything = rag_client.get(f"{PREFIX}/tasks").json()
    assert len(everything["items"]) == 1


def test_task_list_rejects_unknown_status(rag_client: TestClient) -> None:
    """非法 ``status`` → ``400`` 并给出可选值（比空列表好查得多）。"""
    response = rag_client.get(f"{PREFIX}/tasks", params={"status": "NOT_A_STATUS"})

    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "INVALID_ARGUMENT"
    assert "SUCCEEDED" in body["details"]["allowed"]


def test_task_list_is_tenant_scoped(
    rag_client: TestClient, other_user_headers: dict[str, str]
) -> None:
    """任务列表按 ``user_id`` 隔离（``REQ-TASK`` 的隔离边界）。"""
    kb = create_kb(rag_client)
    accepted = upload_text(rag_client, kb["id"], "别人的任务不该被看见。" * 10)
    wait_task(rag_client, accepted["task_id"])

    listing = rag_client.get(f"{PREFIX}/tasks", headers=other_user_headers).json()
    assert listing["items"] == []

    detail = rag_client.get(f"{PREFIX}/tasks/{accepted['task_id']}", headers=other_user_headers)
    assert detail.status_code == 404
    assert detail.json()["error"]["code"] == "TASK_NOT_FOUND"


def test_task_not_found(rag_client: TestClient) -> None:
    """不存在的任务 → ``404 TASK_NOT_FOUND``。"""
    response = rag_client.get(f"{PREFIX}/tasks/task_01M3KGRC3HQ38BN4HEPYRCK73Q")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_cancel_pending_task(make_rag_client: Callable[..., tuple[Any, Any, TestClient]]) -> None:
    """``task_runner=none`` 下任务停在待执行态，可以取消（``docs/08`` §4.3）。"""
    _, _, rag_client = make_rag_client(task_runner="none")
    with rag_client:
        kb = create_kb(rag_client)
        accepted = upload_text(rag_client, kb["id"], "还没开始执行的任务。" * 10)

        before = get_task(rag_client, accepted["task_id"])
        assert before["cancelable"] is True

        canceled = rag_client.post(f"{PREFIX}/tasks/{accepted['task_id']}/cancel")
        assert canceled.status_code == 200
        body = canceled.json()
        assert body["status"] == "CANCELED"
        assert body["finished_at"]
        assert body["cancelable"] is False

        # 重复取消幂等：不再报 409，也不再产生新状态
        again = rag_client.post(f"{PREFIX}/tasks/{accepted['task_id']}/cancel")
        assert again.status_code == 200
        assert again.json()["status"] == "CANCELED"
        assert again.json()["updated_at"] == body["updated_at"]


def test_cancel_terminal_task_conflicts(rag_client: TestClient) -> None:
    """终态任务取消 → ``409 TASK_NOT_CANCELABLE``。"""
    kb = create_kb(rag_client)
    accepted = upload_text(rag_client, kb["id"], "已经成功的任务。" * 10)
    wait_task(rag_client, accepted["task_id"])

    response = rag_client.post(f"{PREFIX}/tasks/{accepted['task_id']}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_NOT_CANCELABLE"


def test_cancel_unknown_task(rag_client: TestClient) -> None:
    """取消不存在的任务 → ``404``。"""
    response = rag_client.post(f"{PREFIX}/tasks/task_01M3KGRC3HQ38BN4HEPYRCK73Q/cancel")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


def test_retry_failed_task_reruns_pipeline(rag_client: TestClient) -> None:
    """失败任务可重试：``retry_count`` 递增并重新执行（最终仍失败，因为内容没变）。"""
    kb = create_kb(rag_client)
    failed = _failing_task(rag_client, kb["id"])
    assert failed["retryable"] is True

    retried = rag_client.post(f"{PREFIX}/tasks/{failed['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["retry_count"] == failed["retry_count"] + 1

    final = wait_task(rag_client, failed["id"])
    assert final["status"] == "FAILED"
    assert final["error"]["code"] == "UNPROCESSABLE_DOCUMENT"


def test_retry_exhausted_returns_conflict(
    make_rag_client: Callable[..., tuple[Any, Any, TestClient]],
) -> None:
    """``retry_count`` 达到 ``max_retries`` 后再重试 → ``409 TASK_NOT_RETRYABLE``。"""
    _, _, rag_client = make_rag_client(task_max_retries=1)
    with rag_client:
        kb = create_kb(rag_client)
        failed = _failing_task(rag_client, kb["id"])

        first = rag_client.post(f"{PREFIX}/tasks/{failed['id']}/retry")
        assert first.status_code == 200
        wait_task(rag_client, failed["id"])

        exhausted = rag_client.post(f"{PREFIX}/tasks/{failed['id']}/retry")
        assert exhausted.status_code == 409
        assert exhausted.json()["error"]["code"] == "TASK_NOT_RETRYABLE"


def test_retry_succeeded_task_conflicts(rag_client: TestClient) -> None:
    """成功任务重试 → ``409 TASK_NOT_RETRYABLE``（只有失败才需要重试）。"""
    kb = create_kb(rag_client)
    accepted = upload_text(rag_client, kb["id"], "已经成功，不需要重试。" * 10)
    wait_task(rag_client, accepted["task_id"])

    response = rag_client.post(f"{PREFIX}/tasks/{accepted['task_id']}/retry")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_NOT_RETRYABLE"


def test_retry_pending_task_conflicts(
    make_rag_client: Callable[..., tuple[Any, Any, TestClient]],
) -> None:
    """未开始执行的任务重试 → ``409 TASK_NOT_RETRYABLE``（``docs/08`` §4.4 仅允许 FAILED）。

    ``QUEUED/RUNNING`` 的幂等返回由服务层单测覆盖（这里造不出「排着队但没跑」
    的 HTTP 状态）。两者不能混为一谈：前者是「重试本就不适用于这种状态」，
    后者是「你刚才已经重试过了」。
    """
    _, _, rag_client = make_rag_client(task_runner="none")
    with rag_client:
        kb = create_kb(rag_client)
        accepted = upload_text(rag_client, kb["id"], "尚未执行的任务。" * 10)

        response = rag_client.post(f"{PREFIX}/tasks/{accepted['task_id']}/retry")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_NOT_RETRYABLE"


# ---------------------------------------------------------------------------
# 进度流（SSE，``docs/08`` §4.5）
# ---------------------------------------------------------------------------
# 这里**只测会自己结束的流**（订阅前已是终态）。原因不是懒：``TestClient`` 的
# 传输层会先把应用跑到生成器结束、再把响应交给测试，所以「永不结束」的流
# （在跑的任务、``FAILED`` 等自动重试）会让请求本身死锁，而不是测出一个结果。
# 「连接保持 + 增量推送」由 ``tests/unit/test_task_stream.py``（逐帧断言、可注入
# 时钟）与真实 uvicorn 上的手工验收共同覆盖，两条线合起来才是完整的契约。


def _events(body: str) -> list[tuple[str, str]]:
    """把 SSE 文本解析成 ``(event, data)`` 列表（与对话流的解析方式一致）。"""
    parsed: list[tuple[str, str]] = []
    for block in body.strip().split("\n\n"):
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if name:
            parsed.append((name, data))
    return parsed


def test_events_stream_headers(rag_client: TestClient) -> None:
    """``AC-TASK-05``：``text/event-stream`` + 禁缓存 + 禁代理缓冲。

    这三个头少一个，接口在本机会「看起来正常」，一挂 Nginx 就变成
    「流式接口不流式」（全部内容缓冲到最后一次性吐出）。
    """
    kb = create_kb(rag_client)
    accepted = upload_text(rag_client, kb["id"], "完成后订阅进度流。" * 10)
    wait_task(rag_client, accepted["task_id"])

    with rag_client.stream("GET", f"{PREFIX}/tasks/{accepted['task_id']}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert response.headers["x-accel-buffering"] == "no"


def test_events_stream_done_for_terminal_task(rag_client: TestClient) -> None:
    """订阅前已是终态 → 立即推一帧 ``done`` 并关闭（不挂住连接）。"""
    kb = create_kb(rag_client)
    accepted = upload_text(rag_client, kb["id"], "已经成功的任务，订阅即结束。" * 10)
    wait_task(rag_client, accepted["task_id"])

    with rag_client.stream("GET", f"{PREFIX}/tasks/{accepted['task_id']}/events") as response:
        body = "".join(response.iter_text())

    events = _events(body)
    assert [name for name, _ in events] == ["done"]
    assert json.loads(events[0][1])["status"] == "SUCCEEDED"


def test_events_stream_done_for_canceled_task(
    make_rag_client: Callable[..., tuple[Any, Any, TestClient]],
) -> None:
    """取消后的任务同样立即得到 ``done``（终态一律如此）。"""
    _, _, rag_client = make_rag_client(task_runner="none")
    with rag_client:
        kb = create_kb(rag_client)
        accepted = upload_text(rag_client, kb["id"], "先取消再订阅。" * 10)
        assert rag_client.post(f"{PREFIX}/tasks/{accepted['task_id']}/cancel").status_code == 200

        with rag_client.stream("GET", f"{PREFIX}/tasks/{accepted['task_id']}/events") as response:
            body = "".join(response.iter_text())

    events = _events(body)
    assert [name for name, _ in events] == ["done"]
    assert json.loads(events[0][1])["status"] == "CANCELED"


def test_events_stream_cross_user_404(
    rag_client: TestClient, other_user_headers: dict[str, str]
) -> None:
    """越权订阅别人任务的进度 → ``404``（而不是一个空的 200 流）。"""
    kb = create_kb(rag_client)
    accepted = upload_text(rag_client, kb["id"], "别人的进度不该被订阅。" * 10)
    wait_task(rag_client, accepted["task_id"])

    response = rag_client.get(
        f"{PREFIX}/tasks/{accepted['task_id']}/events", headers=other_user_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


def test_events_stream_unknown_task_404(rag_client: TestClient) -> None:
    """不存在的任务 → ``404``，而不是一个 200 的空流。"""
    response = rag_client.get(f"{PREFIX}/tasks/task_01M3KGRC3HQ38BN4HEPYRCK73Q/events")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


def test_events_stream_requires_auth(app: FastAPI) -> None:
    """未鉴权 → ``401``（进度信息同样属于用户数据）。"""
    with TestClient(app) as client:
        response = client.get(f"{PREFIX}/tasks/task_01M3KGRC3HQ38BN4HEPYRCK73Q/events")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 过载保护（``docs/08`` §174 / ``docs/10`` §3.3）
# ---------------------------------------------------------------------------


def test_upload_rejects_when_queue_is_full(
    make_rag_client: Callable[..., tuple[Any, Any, TestClient]],
) -> None:
    """未结束任务数已达 ``INGEST_QUEUE_MAX`` → ``503 OVERLOADED``（可退避重试）。

    ``task_runner=none`` 是为了让第一个任务一直停在 ``PENDING``（否则它瞬间成功，
    队列永远是空的）。上限 1 时第二个请求就被拒 —— ``queue_max`` 是「同时最多几个
    未结束任务」，不是「最多 ``queue_max + 1`` 个」。
    """
    _, _, rag_client = make_rag_client(task_runner="none", ingest_queue_max=1)
    with rag_client:
        kb = create_kb(rag_client)
        upload_text(rag_client, kb["id"], "第一个任务占满队列。" * 10)

        response = rag_client.post(
            f"{PREFIX}/knowledge-bases/{kb['id']}/documents",
            data={"text": "第二个任务应当被拒。" * 10, "doc_name": "第二个.md"},
        )

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "OVERLOADED"
    assert body["details"]["queue_max"] == 1


def test_upload_passes_when_queue_has_room(
    make_rag_client: Callable[..., tuple[Any, Any, TestClient]],
) -> None:
    """队列有空间时正常创建（过载判断不能误伤正常请求）。"""
    _, _, rag_client = make_rag_client(task_runner="none", ingest_queue_max=10)
    with rag_client:
        kb = create_kb(rag_client)
        accepted = upload_text(rag_client, kb["id"], "队列很空，正常创建。" * 10)
        assert accepted["task_id"]
        assert get_task(rag_client, accepted["task_id"])["status"] == "PENDING"
