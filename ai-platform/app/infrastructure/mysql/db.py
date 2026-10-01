"""关系库（MySQL）访问的共用基础设施：引擎、时间转换、错误分类。

**为什么单独一个模块**：``app/storage``（KB / 文档 / 切片）与 ``app/memory``
（长期记忆）各自持有自己的表定义，但下面三件事必须**只有一份实现**：

1. **引擎**：每个仓储各自 ``create_async_engine`` 会各自持有一个连接池
   （``pool_size`` 默认 5，三四个仓储就是 20 条连接 —— MySQL 的 ``max_connections``
   默认 151，多副本部署时很容易撞上）。所以引擎在这里按 DSN **进程内复用**：
   RAG 仓储与记忆仓储拿到的是同一个池，谁先关都不会把对方的连接抽掉
   （:func:`release_engine` 按引用计数到 0 才真 ``dispose``）。
2. **时间戳的进出格式**：库里的列是 ``DATETIME(3)``（无时区，约定存 UTC），
   而领域实体里的字段是 ``str``（``2026-09-28T16:05:26.866Z``，见
   :func:`app.memory.context_store.now_iso`）。两个方向各写一遍，迟早出现
   「同一个字段，一个仓储读出来是 ``+00:00``、另一个是 ``Z``」——
   而游标比较是按字符串前 19 位以外的部分比大小的（``docs/12`` §2.1 踩过）。
3. **错误分类**：唯一键冲突、表不存在、连不上，这三种要分别映射成
   ``409`` / ``503`` / ``503``。分散在各仓储里写 ``except IntegrityError``
   一定会漏掉某一种，而漏掉的表现是**500**——把「部署忘了建表」报成服务端 bug。

依赖方向：本模块只依赖 ``app.config`` / ``app.core``，不被它们反向依赖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NoReturn

from app.config import Settings
from app.core.errors import AppError, ErrorCode

#: MySQL 的 ``wait_timeout`` 默认 8 小时，但云上/中间件常调到几分钟。
#: 让连接池主动回收，比等 ``pool_pre_ping`` 在每次取连接时兜底更省往返。
POOL_RECYCLE_SECONDS = 1800

#: 列表查询的游标分页：每页多取一条来判断 ``has_more``
FETCH_AHEAD = 1


#: 进程内按 DSN 复用的引擎：``{dsn: (engine, 引用计数)}``。
#: 不直接缓存 ``AsyncEngine`` 本身，是因为「谁负责关」必须说得清 —— 见
#: :func:`release_engine`。
_ENGINES: dict[str, tuple[Any, int]] = {}


def create_engine_from_settings(settings: Settings) -> Any:
    """按 ``MYSQL_DSN`` 取异步引擎（同 DSN 复用同一个连接池）。

    懒导入 ``sqlalchemy``：``INFRA_BACKEND=memory`` 的部署不该因为没装驱动而起不来
    （与 ``app/storage/objectstore.py`` 对 ``minio`` 的口径一致）。

    Raises:
        AppError: 未安装 ``sqlalchemy`` / 异步驱动时给明确的 ``503``，而不是 ``ImportError``。
    """
    key = settings.mysql_dsn
    cached = _ENGINES.get(key)
    if cached is not None:
        engine, refs = cached
        _ENGINES[key] = (engine, refs + 1)
        return engine
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
    except ImportError as exc:  # pragma: no cover - 需要真实部署才走到
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "未安装 sqlalchemy，无法使用 INFRA_BACKEND=real 的 MySQL 仓储（uv sync --extra mysql）",
        ) from exc
    engine = create_async_engine(
        key,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
    )
    _ENGINES[key] = (engine, 1)
    return engine


async def release_engine(engine: Any) -> None:
    """归还一个引擎引用；计数归零时才真关连接池。

    为什么不在每个仓储的 ``aclose`` 里直接 ``dispose``：RAG 仓储与记忆仓储
    共享同一个引擎，先关的那个会把还活着的那个的连接一起断掉 —— 在 lifespan
    关闭阶段看不出来（反正都要退出），但在测试里（先关一个子系统、再断言
    另一个）会变成奇怪的 ``ConnectionDoesNotExistError``。
    """
    for key, (cached, refs) in list(_ENGINES.items()):
        if cached is engine:
            if refs > 1:
                _ENGINES[key] = (cached, refs - 1)
                return
            _ENGINES.pop(key, None)
            break
    await engine.dispose()


async def aclose_all_engines() -> None:
    """关闭并丢弃全部缓存引擎（lifespan 收尾 / 测试清理用）。"""
    for _, (engine, _refs) in list(_ENGINES.items()):
        await engine.dispose()
    _ENGINES.clear()


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------


def _truncate_millis(moment: datetime) -> datetime:
    """截断到毫秒（与列精度 ``DATETIME(3)`` 对齐）。

    不截断也能存进去（MySQL 会自己截），但那样「写进去的值」与「读回来的值」
    在内存里不同 —— 一次 ``save`` 之后 ``updated_at`` 会跳变，这种不确定性
    会让「乐观锁/幂等」类断言变得难以解释。
    """
    return moment.replace(microsecond=(moment.microsecond // 1000) * 1000)


def db_now() -> datetime:
    """当前 UTC 时刻（naive ``datetime``，列精度毫秒）。"""
    return _truncate_millis(datetime.now(UTC).replace(tzinfo=None))


def to_db(value: str | datetime | None) -> datetime | None:
    """领域时间戳 → 库值。

    接受 ``...Z`` / ``+00:00`` / 已经是 ``datetime`` 三种入参：调用方拿到的
    可能是游标解出来的 ``datetime``，也可能是实体里的字符串，两者都必须能存。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    else:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    # 统一转成 UTC 的 naive：库里存的是 UTC，带偏移的值必须先换算再丢时区
    return _truncate_millis(moment.astimezone(UTC).replace(tzinfo=None))


def from_db(value: datetime | str | None) -> str | None:
    """库值 → 领域时间戳（``2026-09-28T16:05:26.866Z``）。

    格式与 :func:`app.memory.context_store.now_iso` **完全一致**：同一个字段
    在 ``memory`` 与 ``real`` 两种后端下必须长得一样，否则契约测试通过、
    真机联调时前端解析失败。
    """
    if value is None:
        return None
    if isinstance(value, str):  # 某些驱动/生成列会回字符串
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        moment = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    else:
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------

#: 唯一键名 → 领域错误。键名里的表前缀（``knowledge_base.uk_kb_user_name``）用子串匹配，
#: 因为不同 MySQL 版本/驱动对 ``Duplicate entry ... for key`` 的措辞不完全一样。
_DUPLICATE_KEYS: dict[str, tuple[ErrorCode, str]] = {
    "uk_kb_user_name": (ErrorCode.KB_NAME_CONFLICT, "知识库名称已存在"),
    "uk_doc_dedupe": (ErrorCode.DOCUMENT_DUPLICATE, "相同内容的文档已存在"),
    # 记忆的唯一键通常不会跑到这里：``MySqlMemoryRepo.add`` 会自己把它降级成
    # 「命中已有记忆」。但凡是「并发下第一个事务还没提交、重查也查不到」的窗口，
    # 最后一步就得靠它 —— 报 409 虽然不如「命中」漂亮，但比 500 好得多。
    "uk_mem_user_hash": (ErrorCode.CONFLICT, "已存在相同内容的记忆"),
}

#: ``1049 = Unknown database`` / ``1146 = Table doesn't exist`` / ``1054 = Unknown column``
#: —— 都是**部署与代码不一致**，不是「数据写错了」。它们必须报 503 而不是 500：
#: ``docs/10`` 的依赖故障矩阵把「MySQL 不可用」定义为「写类接口 503」，
#: 而「忘了跑建表脚本」在运维眼里就是同一类问题（照着 503 的提示去执行脚本即可）。
_MISSING_SCHEMA_ERRNOS = frozenset({1049, 1146, 1054})

#: 连不上 / 认证失败 / 库不存在
_UNAVAILABLE_ERRNOS = frozenset({2002, 2003, 2006, 2013, 1045, 1044})


def _errno_of(exc: BaseException) -> int | None:
    """取出 DBAPI 层错误码（``pymysql``/``asyncmy`` 都放在 ``args[0]``）。

    先看 ``exc.orig``：SQLAlchemy 会把 DBAPI 异常包一层，包装对象的 ``args``
    里未必有 errno，而真码在 ``orig`` 上。两处都看一遍的成本很低，
    漏看的代价是「表不存在」被当成未知错误报 500。
    """
    for candidate in (getattr(exc, "orig", None), exc):
        args = getattr(candidate, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
    return None


def _text_of(exc: BaseException) -> str:
    """错误文本（``IntegrityError`` 的键名只能从文本里取）。"""
    parts = [str(arg) for arg in getattr(exc, "args", ()) if isinstance(arg, str)]
    origin = getattr(exc, "orig", None)
    if origin is not None:
        parts.extend(str(arg) for arg in getattr(origin, "args", ()) if isinstance(arg, str))
        parts.append(str(origin))
    return " ".join(part for part in parts if part) or str(exc)


def classify_db_error(exc: BaseException) -> AppError | None:
    """把 SQLAlchemy/DBAPI 异常映射成领域错误；无法识别时返回 ``None``。

    返回 ``None`` 而不是「兜底成 INTERNAL_ERROR」：调用方需要知道
    「这条没有被识别」才能决定是否继续往上抛（保留原始堆栈做 500 + 日志）。
    """
    from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, ProgrammingError

    if isinstance(exc, IntegrityError):
        text = _text_of(exc)
        for key, (code, message) in _DUPLICATE_KEYS.items():
            if key in text:
                return AppError(code, message, {"constraint": key})
        # 未登记的唯一键：保留 503 口径（说明代码与表结构不一致），并把它标出来
        return AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "数据库约束冲突（未登记的约束，请检查表结构与代码是否一致）",
            {"constraint": "unknown", "detail": text[:200]},
        )
    if isinstance(exc, (ProgrammingError, OperationalError, DBAPIError)):
        errno = _errno_of(exc)
        if errno in _MISSING_SCHEMA_ERRNOS:
            return AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "MySQL 表结构缺失或与代码不一致：请先执行 deploy/mysql/001_init_schema.sql",
                {"component": "mysql", "errno": errno, "detail": _text_of(exc)[:200]},
            )
        if errno in _UNAVAILABLE_ERRNOS:
            return AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "MySQL 不可用",
                {"component": "mysql", "errno": errno},
            )
    return None


def reraise(exc: BaseException) -> NoReturn:
    """把可识别的库错误转成 ``AppError``，其余原样抛出。

    返回类型是 ``NoReturn`` 而不是 ``None``：调用方常在「本该已经抛了」的分支
    末尾写 ``reraise(exc)``，而 ``-> None`` 会让类型检查认为那里可能**隐式返回**
    （mypy: ``Implicit return in function which does not return``）。
    """
    mapped = classify_db_error(exc)
    if mapped is not None:
        raise mapped from exc
    raise exc


__all__ = [
    "FETCH_AHEAD",
    "POOL_RECYCLE_SECONDS",
    "aclose_all_engines",
    "classify_db_error",
    "create_engine_from_settings",
    "db_now",
    "from_db",
    "release_engine",
    "reraise",
    "to_db",
]
