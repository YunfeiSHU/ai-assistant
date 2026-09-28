"""RAG 契约测试的公共动作（建库 / 上传 / 等任务）。

**为什么把「等任务」写成轮询而不是 ``sleep(0.5)``**：入库是真正的异步流水线，
固定睡眠在慢机器上会偶发失败、在快机器上白等。轮询 + 明确超时让用例既稳定又
能在失败时给出「任务卡在哪个状态」这个信息（`docs/11` §2.2 要求集成用例失败
时能直接定位）。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

#: 任务终态（与 ``app/tasks/models.py::TERMINAL_STATUSES`` 同源语义）
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELED"})

DEFAULT_TIMEOUT = 10.0


def create_kb(client: TestClient, **overrides: Any) -> dict[str, Any]:
    """建一个知识库并返回响应体。"""
    payload: dict[str, Any] = {"name": "测试知识库", **overrides}
    response = client.post("/api/v1/knowledge-bases", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def upload_text(
    client: TestClient,
    kb_id: str,
    text: str,
    *,
    doc_name: str = "测试文档",
    **form: Any,
) -> dict[str, Any]:
    """以纯文本方式上传，返回 ``UploadAccepted`` 响应体。"""
    data: dict[str, Any] = {"text": text, "doc_name": doc_name, **form}
    response = client.post(f"/api/v1/knowledge-bases/{kb_id}/documents", data=data)
    assert response.status_code == 202, response.text
    return response.json()


def upload_file(
    client: TestClient,
    kb_id: str,
    content: bytes,
    *,
    filename: str,
    content_type: str = "text/markdown",
    **form: Any,
) -> dict[str, Any]:
    """以文件方式上传，返回 ``UploadAccepted`` 响应体。"""
    data: dict[str, Any] = {**form}
    files = {"file": (filename, content, content_type)}
    response = client.post(f"/api/v1/knowledge-bases/{kb_id}/documents", data=data, files=files)
    assert response.status_code == 202, response.text
    return response.json()


def get_task(client: TestClient, task_id: str) -> dict[str, Any]:
    """取任务详情。"""
    response = client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_task(
    client: TestClient, task_id: str, *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """轮询直到任务进入终态。

    超时直接把最后一次看到的状态放进断言消息 —— 否则失败信息只有「超时」，
    而「卡在 EMBEDDING」与「根本没开始」是完全不同的两个问题。
    """
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = get_task(client, task_id)
        if last["status"] in TERMINAL_STATUSES:
            return last
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未进入终态，最后状态：{last}")


def wait_task_success(
    client: TestClient, task_id: str, *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """等到成功；失败时把 ``error`` 一起断言出来。"""
    task = wait_task(client, task_id, timeout=timeout)
    assert task["status"] == "SUCCEEDED", f"任务失败：{task.get('error')}"
    return task


def wait_document_status(
    client: TestClient,
    doc_id: str,
    expected: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """轮询文档状态（入库是异步的，上传响应里看不到最终状态）。"""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/documents/{doc_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] == expected:
            return last
        time.sleep(0.02)
    raise AssertionError(f"文档 {doc_id} 未在 {timeout}s 内变为 {expected}，最后：{last}")


def ingest_text(
    client: TestClient,
    kb_id: str,
    text: str,
    *,
    doc_name: str = "测试文档",
    **form: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """上传并等到入库完成，返回 ``(上传响应, 文档详情)``。"""
    accepted = upload_text(client, kb_id, text, doc_name=doc_name, **form)
    wait_task_success(client, accepted["task_id"])
    document = wait_document_status(client, accepted["doc_id"], "INDEXED")
    return accepted, document


def search(client: TestClient, kb_id: str, query: str, **body: Any) -> dict[str, Any]:
    """调用检索调试接口。"""
    response = client.post(f"/api/v1/knowledge-bases/{kb_id}/search", json={"query": query, **body})
    assert response.status_code == 200, response.text
    return response.json()


__all__ = [
    "DEFAULT_TIMEOUT",
    "TERMINAL_STATUSES",
    "create_kb",
    "get_task",
    "ingest_text",
    "search",
    "upload_file",
    "upload_text",
    "wait_document_status",
    "wait_task",
    "wait_task_success",
]
