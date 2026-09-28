"""结构守卫：``logger.*(..., extra={...})`` 的键名不能与 ``LogRecord`` 内置属性冲突。

**这条守卫来自一个真实线上故障。** ``app/tasks/service.py`` 里写过
``logger.info("task.created", extra={"created": ...})``，而 ``LogRecord`` 自带
``created``（记录时间戳）。CPython 的 ``Logger.makeRecord`` 会直接抛
``KeyError: "Attempt to overwrite 'created' in LogRecord"``。

阴险之处在于它**只在日志级别允许输出时才会触发**：

* 测试里 ``LOG_LEVEL=WARNING`` → ``isEnabledFor(INFO)`` 为假 → ``makeRecord`` 根本不执行
  → 全绿；
* 生产 ``LOG_LEVEL=INFO`` → 每次创建任务都抛 ``KeyError`` → 建任务接口直接 500。

也就是「测试全过、上线就炸」。这类坑靠单个用例防不住（要写得对还得刚好开着 INFO 日志），
只能靠结构化检查：把全仓的 ``extra=`` 键名和 ``LogRecord`` 的字段名对一遍。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: ``LogRecord`` 构造后就有的属性名（用标准库自己生成，避免手抄一份过时清单）。
#: ``message``/``asctime`` 不在初始 dict 里，但同样会被 ``makeRecord`` 的检查拦下。
RESERVED_KEYS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _iter_extra_keys() -> list[tuple[str, int, str]]:
    """遍历 ``app/`` 下所有 ``extra={...}``，产出 ``(文件, 行号, 键名)``。"""
    found: list[tuple[str, int, str]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(APP_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.append((relative, key.lineno, key.value))
    return found


def test_at_least_one_extra_is_scanned() -> None:
    """守卫本身要能"看见"目标：扫不到任何 ``extra=`` 就说明匹配逻辑坏了。"""
    assert _iter_extra_keys(), "没有扫到任何 extra={...}，结构化检查可能已失效"


def test_extra_keys_do_not_shadow_logrecord() -> None:
    """``extra=`` 的键名不得与 ``LogRecord`` 属性重名。"""
    collisions = [
        f"{path}:{line} 使用了保留键 {key!r}"
        for path, line, key in _iter_extra_keys()
        if key in RESERVED_KEYS
    ]
    assert not collisions, (
        "extra= 键名与 LogRecord 内置属性冲突，会在日志级别允许输出时抛 KeyError"
        "（表现为测试全绿、生产报错）：\n" + "\n".join(collisions)
    )


def test_reserved_keys_cover_the_known_offender() -> None:
    """保留清单必须包含本次踩坑的 ``created``（防止清单被误改空）。"""
    assert {"created", "message", "asctime", "levelname"} <= RESERVED_KEYS


@pytest.mark.parametrize("key", ["created", "message", "asctime", "funcName", "process"])
def test_make_record_actually_rejects_reserved_keys(key: str) -> None:
    """反向验证：这些键确实会被标准库拒绝（说明我们不是在凭空设限）。"""
    record = logging.LogRecord("n", logging.INFO, __file__, 1, "m", None, None)

    with pytest.raises(KeyError):
        logging.Logger("guard").makeRecord(
            "n", logging.INFO, __file__, 1, "m", None, None, extra={key: "x"}
        )
    assert key in record.__dict__ or key in {"message", "asctime"}
