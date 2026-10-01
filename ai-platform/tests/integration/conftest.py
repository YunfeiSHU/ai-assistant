"""集成层夹具（``docs/11`` §2 的第三层）：需要**真实容器**的用例。

这一层解决的是一类单测**原理上**覆盖不到的问题：把内存替身换成真实现之后，
我们写的到底是「一段能跑通的命令」还是「一段到了 Redis 上语义不对的命令」。
Redis 里能踩的坑（Lua 脚本的原子性、``SET NX`` 的幂等、ZSET 的排序与游标、
pub/sub 的「订阅前发的帧不会重放」）都只在真实连接上才暴露。

M8 之后这一层还多了三个依赖：

* **MySQL** —— :mod:`app.infrastructure.storage.mysql` / :mod:`app.memory.mysql_repo` 的
  «SQL 写得对不对»（行值游标、唯一键冲突映射、``hit_count`` 的原子自增）；
* **Milvus** —— :mod:`app.memory.milvus_index` 的 «集合建得对不对»
  （标量索引存不存在、``user_id`` 过滤有没有生效）；
* **MinIO** —— :class:`~app.infrastructure.storage.objectstore.MinioObjectStore` 的上传/下载/删除。

约定：

* 地址取环境变量（``AI_TEST_REDIS_URL`` / ``AI_TEST_MYSQL_DSN`` /
  ``AI_TEST_MILVUS_URI`` / ``AI_TEST_MINIO_ENDPOINT``），都有本地默认值；
* Redis 每个用例开始前 ``FLUSHDB``，所以**默认只允许指向非 0 号库**
  （防手滑清掉开发数据）；
* MySQL **不用「删库」来隔离**：每个用例拿一个唯一的 ``user_id``
  （``u_01J...``），结束时只删自己那几行。理由是这个库就是本机开发库，
  而「为了跑测试去建一个新库」还得再执行一遍 001 建表脚本 —— 代价大于收益。
  代价是：测试留下的痕迹必须靠 ``cleanup_user`` 收干净（见该夹具）。
* 依赖不可用 → ``pytest.skip``（本机没容器时整层自动跳过，而不是报红）。

起容器/服务：``docker compose -f ai-platform/deploy/infra/compose.yml up -d``
（Redis + Kafka + MinIO）；MySQL 用本机实例；Milvus 用外部 standalone。
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import pytest
from tests.conftest import build_settings

from app.core.config import Settings
from app.core.ids import new_id

REDIS_URL_ENV = "AI_TEST_REDIS_URL"
DEFAULT_REDIS_URL = "redis://localhost:6379/15"

#: MySQL：指向本机开发库。测试靠 ``user_id`` 隔离，不做删库操作。
MYSQL_DSN_ENV = "AI_TEST_MYSQL_DSN"
DEFAULT_MYSQL_DSN = "mysql+asyncmy://root:20050613@localhost:3306/ai_platform"

MILVUS_URI_ENV = "AI_TEST_MILVUS_URI"
DEFAULT_MILVUS_URI = "http://localhost:19530"

MINIO_ENDPOINT_ENV = "AI_TEST_MINIO_ENDPOINT"
DEFAULT_MINIO_ENDPOINT = "localhost:9000"
DEFAULT_MINIO_ACCESS_KEY = "minioadmin"
DEFAULT_MINIO_SECRET_KEY = "minioadmin"

#: 可用性探测超时（秒）。redis-py 自己的连接重试会退避到 ~4s，19 条用例就是一分多钟
#: —— 集成层「没容器就跑不起来」是常态，为此赔上一分钟不合适。
PROBE_TIMEOUT = 0.5


def _db_index(url: str) -> int:
    """取 URL 里的库号（``redis://host:port/15`` → 15，缺省 0）。"""
    path = urlsplit(url).path.lstrip("/")
    return int(path) if path.isdigit() else 0


@lru_cache(maxsize=8)
def _reachable(host: str, port: int) -> bool:
    """TCP 探测一次（``lru_cache`` 保证整轮测试只探一次）。"""
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def redis_url() -> str:
    """集成层 Redis 地址。

    **必须是独立库**：夹具会 ``FLUSHDB``，指向 0 号库等于清掉本地开发数据。
    这里直接跳过而不是替你改成 15 —— 悄悄换掉用户指定的地址更难排查。
    """
    url = os.getenv(REDIS_URL_ENV, DEFAULT_REDIS_URL)
    if _db_index(url) == 0:
        pytest.skip(
            f"{REDIS_URL_ENV}={url} 指向 0 号库：本层每个用例都会 FLUSHDB，"
            f"请指向专用库（默认 {DEFAULT_REDIS_URL}）"
        )
    host = urlsplit(url).hostname or "localhost"
    if not _reachable(host, urlsplit(url).port or 6379):
        pytest.skip(
            f"Redis 不可达（{url}）：起容器 docker compose -f deploy/infra/compose.yml up -d redis"
        )
    return url


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Any]:
    """连上 Redis 并清空专用库（每个用例一份干净状态）。"""
    redis = pytest.importorskip("redis.asyncio", reason="未安装 redis（uv sync --extra redis）")
    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )
    await client.ping()
    await client.flushdb()
    try:
        yield client
    finally:
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is not None:
            await closer()


# ---------------------------------------------------------------------------
# MySQL（app.infrastructure.storage.mysql / app.memory.mysql_repo）
# ---------------------------------------------------------------------------


def _split_dsn(dsn: str) -> tuple[str, int, str]:
    """``mysql+asyncmy://user:pw@host:port/db`` → ``(host, port, db)``。"""
    tail = dsn.split("@")[-1]
    hostport, _, database = tail.partition("/")
    host, _, port = hostport.partition(":")
    return host or "localhost", int(port or 3306), database.split("?")[0]


@pytest.fixture(scope="session")
def mysql_dsn() -> str:
    """集成层 MySQL DSN（不可达则整层跳过）。"""
    dsn = os.getenv(MYSQL_DSN_ENV, DEFAULT_MYSQL_DSN)
    host, port, _ = _split_dsn(dsn)
    if not _reachable(host, port):
        pytest.skip(f"MySQL 不可达（{host}:{port}）：本文档的 MySQL 用例需要真实实例")
    return dsn


@pytest.fixture(scope="session")
def mysql_settings(mysql_dsn: str) -> Settings:
    """``INFRA_BACKEND=real`` + 真实 DSN 的配置。"""
    return build_settings(infra_backend="real", mysql_dsn=mysql_dsn)


@pytest.fixture
def test_user() -> str:
    """本用例专属的 ``user_id``（集成层就是靠它隔离数据的）。"""
    return new_id("u")


@pytest.fixture
async def rag_repos(mysql_settings: Settings) -> AsyncIterator[Any]:
    """真实 MySQL 的 KB / 文档 / 切片仓储门面。"""
    from app.infrastructure.storage.mysql import MySqlRagRepository

    facade = MySqlRagRepository(mysql_settings)
    try:
        yield facade
    finally:
        await facade.aclose()


@pytest.fixture
async def cleanup_user(mysql_settings: Settings, test_user: str) -> AsyncIterator[str]:
    """用例结束后删掉该 ``user_id`` 在本层涉及的四张表里的行。

    只按 ``user_id`` 删 —— 开发库里可能有真实数据，``TRUNCATE``/``DELETE`` 全表
    是绝对不能出现的操作（``docs/12`` 的教训就是「删错东西时往往已经来不及」）。
    """
    yield test_user
    from sqlalchemy import text

    from app.infrastructure.mysql.db import create_engine_from_settings, release_engine

    engine = create_engine_from_settings(mysql_settings)
    try:
        async with engine.begin() as connection:
            # 先子表后父表：外键方向是 chunk → document → knowledge_base
            await connection.execute(
                text("DELETE FROM document_chunk WHERE user_id = :uid"), {"uid": test_user}
            )
            await connection.execute(
                text("DELETE FROM document WHERE user_id = :uid"), {"uid": test_user}
            )
            await connection.execute(
                text("DELETE FROM knowledge_base WHERE user_id = :uid"), {"uid": test_user}
            )
            await connection.execute(
                text("DELETE FROM user_memory WHERE user_id = :uid"), {"uid": test_user}
            )
    finally:
        await release_engine(engine)


# ---------------------------------------------------------------------------
# Milvus（app.memory.milvus_index）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def milvus_settings() -> Settings:
    """指向真实 Milvus 的配置（不可达则跳过依赖它的用例）。

    凭据留空：本机 standalone 默认不开鉴权（``AI_TEST_MILVUS_USER`` 可覆盖）。
    """
    uri = os.getenv(MILVUS_URI_ENV, DEFAULT_MILVUS_URI)
    parsed = urlsplit(uri)
    if not _reachable(parsed.hostname or "localhost", parsed.port or 19530):
        pytest.skip(f"Milvus 不可达（{uri}）：记忆向量索引用例需要真实实例")
    return build_settings(
        infra_backend="real",
        milvus_uri=uri,
        milvus_user=os.getenv("AI_TEST_MILVUS_USER", ""),
        milvus_password=os.getenv("AI_TEST_MILVUS_PASSWORD", ""),
    )


@pytest.fixture
async def memory_index(milvus_settings: Settings) -> AsyncIterator[Any]:
    """已 ``ensure_ready()`` 的 Milvus 记忆向量索引。"""
    from app.memory.milvus_index import MilvusMemoryVectorIndex

    index = MilvusMemoryVectorIndex(milvus_settings)
    await index.ensure_ready()
    try:
        yield index
    finally:
        closer = getattr(index._client, "close", None)
        if closer is not None:
            closer()


# ---------------------------------------------------------------------------
# MinIO（app.infrastructure.storage.objectstore）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def minio_settings() -> Settings:
    """指向真实 MinIO 的配置（不可达则跳过依赖它的用例）。"""
    endpoint = os.getenv(MINIO_ENDPOINT_ENV, DEFAULT_MINIO_ENDPOINT)
    host, _, port = endpoint.partition(":")
    if not _reachable(host or "localhost", int(port or 9000)):
        pytest.skip(f"MinIO 不可达（{endpoint}）：对象存储用例需要真实实例")
    return build_settings(
        infra_backend="real",
        minio_endpoint=endpoint,
        minio_access_key=os.getenv("AI_TEST_MINIO_ACCESS_KEY", DEFAULT_MINIO_ACCESS_KEY),
        minio_secret_key=os.getenv("AI_TEST_MINIO_SECRET_KEY", DEFAULT_MINIO_SECRET_KEY),
    )


@pytest.fixture
async def object_store(minio_settings: Settings) -> Any:
    """真实 MinIO 对象存储适配器（桶不存在时由实现自动创建）。"""
    from app.infrastructure.storage.objectstore import MinioObjectStore

    return MinioObjectStore(minio_settings)
