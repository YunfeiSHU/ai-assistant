"""结构守卫：``app/`` 下的 Python 源码不得带 UTF-8 BOM。

**这条守卫来自一个真实故障。** ``tools/gen_grpc_stubs.ps1`` 用
``Set-Content -Encoding UTF8`` 写 ``app/grpc/**/__init__.py``，而 PowerShell 5.1
的那个编码**带 BOM**。Python 的 import 机制会容忍 BOM（tokenizer 自己剥掉），
所以 ``import app.grpc`` 一切正常。

但仓库里的结构化检查是用 ``ast.parse(源码文本)`` 读文件的，它**不容忍** BOM：

.. code-block:: text

    SyntaxError: invalid non-printable character U+FEFF

于是失败现场是「日志结构守卫无故报语法错」——而真正的原因在一个
从没被怀疑过的生成脚本里，且只在重新生成 stub 之后才出现。

这类坑靠「记得别用 Set-Content」防不住，只能靠一次性扫全仓。
"""

from __future__ import annotations

import pytest
from test_logging_extras import APP_ROOT

UTF8_BOM = b"\xef\xbb\xbf"


def _python_sources() -> list:
    return sorted(APP_ROOT.rglob("*.py"))


def test_python_sources_exist() -> None:
    """守卫本身要能看见目标：一个文件都扫不到说明路径逻辑坏了。"""
    assert _python_sources(), f"没有在 {APP_ROOT} 下扫到任何 .py"


@pytest.mark.parametrize("path", _python_sources(), ids=lambda p: p.name)
def test_no_utf8_bom(path) -> None:
    """文件首字节不得是 BOM。"""
    head = path.read_bytes()[:3]
    assert head != UTF8_BOM, (
        f"{path.name} 带 UTF-8 BOM：ast.parse 会报 "
        "'invalid non-printable character U+FEFF'，"
        "而 import 却能成功 —— 于是失败现场会指向错误的方向。"
        "写文件请用 [IO.File]::WriteAllText($p, $t, [Text.UTF8Encoding]::new($false))"
    )


@pytest.mark.parametrize("path", _python_sources(), ids=lambda p: p.name)
def test_source_is_valid_utf8(path) -> None:
    """源码必须是合法 UTF-8（PS 5.1 的 ANSI 默认编码会写坏中文）。"""
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - 只在真坏掉时触发
        pytest.fail(f"{path.name} 不是合法 UTF-8：{exc}")
