"""``app/infrastructure/mysql/db.py`` 的单测：时间戳转换、错误分类、引擎共享。

这三件事都属于「写错了不会报错，只会表现成别的问题」的那一类：

* 时间戳格式不一致 → 契约测试照过，前端在真机上解析失败；
* 错误分类漏一种 → 「忘建表」被报成 500，运维去翻代码；
* 引擎不复用 → 每个仓储一个连接池，多副本部署时撞 ``max_connections``。

所以它们值得脱开数据库单独钉住（真库上的行为由 ``tests/integration`` 覆盖）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from tests.conftest import build_settings

from app.core.exceptions import AppError, ErrorCode
from app.infrastructure.mysql.db import (
    aclose_all_engines,
    classify_db_error,
    create_engine_from_settings,
    db_now,
    from_db,
    release_engine,
    reraise,
    to_db,
)


def _integrity(message: str, errno: int = 1062) -> Exception:
    """构造一个 SQLAlchemy ``IntegrityError``（``orig`` 带 errno 与键名）。"""
    from sqlalchemy.exc import IntegrityError

    origin = Exception(errno, message)
    return IntegrityError("INSERT INTO t VALUES (?)", {}, origin)


def _programming(errno: int, message: str) -> Exception:
    from sqlalchemy.exc import ProgrammingError

    return ProgrammingError("SELECT 1", {}, Exception(errno, message))


def _operational(errno: int, message: str) -> Exception:
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT 1", {}, Exception(errno, message))


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------


def test_roundtrip_preserves_millisecond_precision() -> None:
    """``str → DB → str`` 必须逐字符相等（毫秒精度 + ``Z`` 结尾）。"""
    original = "2026-09-28T16:05:26.866Z"
    assert from_db(to_db(original)) == original


def test_to_db_normalises_offsets_to_utc() -> None:
    """带偏移的输入先换算成 UTC 再落库 —— 库里的列是无时区的。"""
    stored = to_db("2026-09-28T18:05:26.866+02:00")
    assert stored == datetime(2026, 9, 28, 16, 5, 26, 866000)


def test_to_db_truncates_to_millis() -> None:
    """微秒被截断：不然「写进去的值」与「读回来的值」在一次 save 后跳变。"""
    stored = to_db(datetime(2026, 9, 28, 16, 5, 26, 866987, tzinfo=UTC))
    assert stored == datetime(2026, 9, 28, 16, 5, 26, 866000)


def test_from_db_accepts_strings_and_none() -> None:
    """驱动/生成列可能回字符串；``None`` 必须原样透传。"""
    assert from_db(None) is None
    assert from_db("2026-09-28 16:05:26.866") == "2026-09-28T16:05:26.866Z"
    assert from_db(datetime(2026, 9, 28, 16, 5, 26, tzinfo=timezone(timedelta(hours=8)))) == (
        "2026-09-28T08:05:26.000Z"
    )


def test_db_now_is_naive_utc_with_millis() -> None:
    """``db_now`` 给的是 naive UTC（列无时区）且毫秒对齐。"""
    moment = db_now()
    assert moment.tzinfo is None
    assert moment.microsecond % 1000 == 0
    assert abs((datetime.now(UTC).replace(tzinfo=None) - moment).total_seconds()) < 5


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


def test_duplicate_kb_name_maps_to_conflict() -> None:
    """同一用户下 KB 重名（``uk_kb_user_name``）要映射成 ``KB_NAME_CONFLICT`` 而不是 500。"""
    mapped = classify_db_error(_integrity("Duplicate entry 'k' for key 'uk_kb_user_name'"))
    assert mapped is not None
    assert mapped.code is ErrorCode.KB_NAME_CONFLICT


def test_duplicate_document_maps_to_duplicate() -> None:
    """命中 ``uk_doc_dedupe``（同 KB 同内容）要映射成 ``DOCUMENT_DUPLICATE``。"""
    mapped = classify_db_error(_integrity("Duplicate entry 'x' for key 'document.uk_doc_dedupe'"))
    assert mapped is not None
    assert mapped.code is ErrorCode.DOCUMENT_DUPLICATE


def test_unregistered_unique_key_is_a_deployment_error() -> None:
    """未登记的唯一键 → 503（说明代码与表结构不同步），而不是 500。"""
    mapped = classify_db_error(_integrity("Duplicate entry 'x' for key 'uk_brand_new'"))
    assert mapped is not None
    assert mapped.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert mapped.details["constraint"] == "unknown"


@pytest.mark.parametrize("errno", [1049, 1146, 1054])
def test_missing_schema_errnos_are_503(errno: int) -> None:
    """库/表/列不存在 → 503 且提示先跑建表脚本（``docs/10`` 的依赖矩阵）。"""
    mapped = classify_db_error(_programming(errno, "Unknown column 'metadata'"))
    assert mapped is not None
    assert mapped.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert "001_init_schema.sql" in mapped.message


@pytest.mark.parametrize("errno", [2002, 2003, 2006, 2013, 1045])
def test_unavailable_errnos_are_503(errno: int) -> None:
    """连不上 / 握手失败 / 连接被断 / 认证失败都归 ``DEPENDENCY_UNAVAILABLE`` 且标出 mysql。"""
    mapped = classify_db_error(_operational(errno, "Can't connect"))
    assert mapped is not None
    assert mapped.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert mapped.details["component"] == "mysql"


def test_unknown_error_is_not_classified() -> None:
    """识别不了就返回 ``None``，让调用方保留原始 500 与堆栈。"""
    assert classify_db_error(RuntimeError("boom")) is None
    assert classify_db_error(_programming(9999, "something else")) is None


def test_reraise_keeps_unclassified_original() -> None:
    """``reraise`` 只转换已分类的错误；认不出来的必须原样抛出，保住 500 与原始堆栈。"""
    original = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        reraise(original)
    with pytest.raises(AppError) as excinfo:
        reraise(_integrity("Duplicate entry 'k' for key 'uk_kb_user_name'"))
    assert excinfo.value.code is ErrorCode.KB_NAME_CONFLICT


# ---------------------------------------------------------------------------
# 引擎共享
# ---------------------------------------------------------------------------


async def test_engine_is_shared_per_dsn_and_released_by_reference_count() -> None:
    """同 DSN 拿到**同一个**引擎；引用计数归零才真关（先关的不能拆掉别人的池）。"""
    # 引擎缓存的引用计数是**进程级**的：其它用例（尤其集成层）可能已经持着
    # 同一个 DSN 的引擎，先清空才能观察到「归零」这个边界。
    await aclose_all_engines()
    settings = build_settings(infra_backend="real")
    first = create_engine_from_settings(settings)
    second = create_engine_from_settings(settings)
    try:
        assert first is second  # 两个仓储家族共享一个连接池
    finally:
        # 归还两次：第一次只减计数，第二次才 dispose
        await release_engine(first)
    await release_engine(second)
    # 缓存已清空 → 再来一次是新引擎（不是被 dispose 的旧对象）
    third = create_engine_from_settings(settings)
    assert third is not first
    await aclose_all_engines()


async def test_release_unknown_engine_still_disposes() -> None:
    """不在缓存里的引擎（调用方自建）也要能关掉，否则连接会漏。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(build_settings(infra_backend="real").mysql_dsn)
    await release_engine(engine)
