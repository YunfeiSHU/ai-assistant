"""长期记忆仓储不可用时的占位实现（降级路径，不阻断启动）。

与 :mod:`app.infrastructure.storage.unavailable` 同一个取舍；这里补充记忆特有的那一层：
记忆是「增强」而非「必需」（``docs/07`` §5.4 允许关闭）。所以 MySQL 初始化失败时正确
行为不是让对话跟着挂掉，而是：

* 所有记忆接口返回 ``503 DEPENDENCY_UNAVAILABLE``（不假装成功）；
* 对话继续可用，并在 ``degraded_reasons`` 里带上 ``memory_unavailable``。

不直接返回内存实现：它「看起来是好的」—— 写进去能读出来、检索也命中，只有重启后消失。
在 ``real`` 部署里这等于静默丢用户数据，而且没有任何一处会报错。
"""

from __future__ import annotations

from app.core.exceptions import AppError, ErrorCode
from app.memory.long_term import (
    MemoryKind,
    MemoryRecord,
    MemoryWriteResult,
)

#: 降级原因（与 :data:`app.infrastructure.storage.unavailable.REASON` 同一措辞口径）
REASON = "长期记忆仓储不可用（INFRA_BACKEND=real 但 MySQL 初始化失败，详见服务端日志）"


def _unavailable(method: str) -> AppError:
    return AppError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        f"memory_repo.{method} 不可用：{REASON}",
        {"component": "mysql", "repo": "memory_repo", "method": method},
    )


class UnavailableMemoryRepo:
    """满足 :class:`~app.memory.long_term.MemoryRepo` 的占位实现。"""

    async def add(self, record: MemoryRecord) -> MemoryWriteResult:
        raise _unavailable("add")

    async def find_by_hash(self, user_id: str, content: str) -> MemoryRecord | None:
        raise _unavailable("find_by_hash")

    async def touch(self, mem_id: str, *, confidence: float = 0.0) -> MemoryRecord:
        raise _unavailable("touch")

    async def get(self, mem_id: str, user_id: str) -> MemoryRecord:
        raise _unavailable("get")

    async def list_page(
        self,
        user_id: str,
        *,
        kind: MemoryKind | None = None,
        expired: bool | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[MemoryRecord], bool]:
        raise _unavailable("list_page")

    async def save(self, record: MemoryRecord) -> MemoryRecord:
        raise _unavailable("save")

    async def delete(self, mem_id: str, user_id: str) -> MemoryRecord:
        raise _unavailable("delete")

    async def delete_all(self, user_id: str) -> int:
        raise _unavailable("delete_all")

    async def count(self, user_id: str, *, active_only: bool = False) -> int:
        raise _unavailable("count")

    async def all_for_user(self, user_id: str) -> list[MemoryRecord]:
        raise _unavailable("all_for_user")


__all__ = ["REASON", "UnavailableMemoryRepo"]
