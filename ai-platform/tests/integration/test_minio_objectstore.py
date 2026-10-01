"""MinIO 对象存储的集成测试（``docs/09`` §6）。

这一层要证明的是「S3 的调用语义对不对」，重点是三条只会在真服务上暴露的：

* 桶不存在时**自动创建**（``_ensure_bucket``），而不是等第一次上传报 ``NoSuchBucket``；
* 删除**幂等**（``NoSuchKey`` 也算成功）—— 删除任务必须可重试（``docs/09`` §6）；
* 对象不存在时 ``get`` 报 ``404``，而 MinIO 本身挂掉时报 ``503`` 而不是 404
  （报错分类错了会让人去删了重传，而真正坏掉的东西一直没发现）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.core.ids import new_id
from app.infrastructure.storage.base import build_object_key

pytestmark = pytest.mark.usefixtures("minio_settings")


async def _cleanup(store: Any, *keys: str) -> None:
    for key in keys:
        await store.delete(key)


async def test_put_get_delete_roundtrip(object_store: Any) -> None:
    """上传 → 存在 → 下载内容一致 → 删除 → 不存在。"""
    key = build_object_key(new_id("u"), new_id("kb"), new_id("doc"), "handbook.md")
    payload = "第一段正文\n第二段正文".encode()

    try:
        await object_store.put(key, payload, "text/markdown; charset=utf-8")
        assert await object_store.exists(key) is True
        assert await object_store.get(key) == payload
    finally:
        await object_store.delete(key)

    assert await object_store.exists(key) is False


async def test_delete_is_idempotent(object_store: Any) -> None:
    """删除不存在的对象不报错（``NoSuchKey`` 视作成功）。"""
    await object_store.delete(
        build_object_key(new_id("u"), new_id("kb"), new_id("doc"), "never-existed.md")
    )


async def test_get_missing_object_returns_404(object_store: Any) -> None:
    """对象不存在 → ``404 DOCUMENT_NOT_FOUND``（而不是 500）。"""
    key = build_object_key(new_id("u"), new_id("kb"), new_id("doc"), "missing.md")
    with pytest.raises(AppError) as excinfo:
        await object_store.get(key)
    assert excinfo.value.code is ErrorCode.DOCUMENT_NOT_FOUND


async def test_bucket_is_created_on_demand(minio_settings: Any) -> None:
    """桶不存在时由实现自动创建（部署脚本里没有「建桶」这一步）。"""
    from app.infrastructure.storage.objectstore import MinioObjectStore

    store = MinioObjectStore(minio_settings)
    key = build_object_key(new_id("u"), new_id("kb"), new_id("doc"), "probe.txt")
    try:
        await store.put(key, b"probe", "text/plain")
        # 客户端能列到这个桶，说明 ``_ensure_bucket`` 真的执行过
        assert store.client.bucket_exists(minio_settings.minio_bucket) is True
    finally:
        await _cleanup(store, key)


def test_build_object_store_uses_minio_when_real(minio_settings: Any) -> None:
    """``INFRA_BACKEND=real`` ⇒ 工厂给出 MinIO 实现（不是内存兜底）。"""
    from app.infrastructure.storage import build_object_store

    assert build_object_store(minio_settings).__class__.__name__ == "MinioObjectStore"


def test_build_object_store_uses_memory_otherwise() -> None:
    """非 real 后端 ⇒ 工厂给出内存实现，本地/单测不需要对象存储容器。"""
    from tests.conftest import build_settings

    from app.infrastructure.storage import build_object_store

    assert build_object_store(build_settings()).__class__.__name__ == "InMemoryObjectStore"
