"""``calculator``：白名单 AST 求值（``docs/04`` §3.1，P0）。

**绝对不用 `eval`。** 模型是**不可信输入源**：它的参数来自用户提问，而用户可以直接
说「请调用 calculator 计算 ``__import__('os').system('rm -rf /')``」。
所以这里是「解析成 AST → 逐节点白名单校验 → 递归求值」，任何白名单外的节点类型
（``Call`` / ``Attribute`` / ``Import`` / ``Lambda`` / 下标 / 比较 / 布尔运算）一律拒绝。

拒绝发生在 **pydantic validator 里**，因此它是一类**参数非法**（``invalid_arguments``），
会被回注给模型让它改参数重试（``AC-AGENT-04``），而不是抛异常把整轮对话打断。
"""

from __future__ import annotations

import ast
import operator
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator

from app.tools.base import BuiltinTool, ToolContext, ToolExecutionError, ToolOutcome

TOOL_NAME = "calculator"
DESCRIPTION = (
    "对算术表达式求值并返回精确结果。参数是纯数学表达式字符串，"
    "只支持 + - * / % ** 与括号、数字常量。"
    "当需要做数值计算（比例、同比增长、单位换算的算术部分）时使用；"
    "查询政策条文或文本内容不要用它。"
)

#: 表达式长度上限（``docs/04`` §3.1）
EXPRESSION_MAX_CHARS = 200
#: 指数上限（``docs/04`` §3.1）：``9**9**9`` 这种会直接吃满 CPU/内存
EXPONENT_MAX = 1000

#: 允许的二元运算符 → 实现
_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
#: 允许的一元运算符 → 实现
_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

#: 白名单外的节点类型的人类可读名（用于错误消息）
_NODE_LABELS: dict[type[ast.AST], str] = {
    ast.Call: "函数调用",
    ast.Attribute: "属性访问",
    ast.Subscript: "下标访问",
    ast.Lambda: "lambda",
    ast.Name: "变量引用",
    ast.Compare: "比较运算",
    ast.BoolOp: "布尔运算",
    ast.IfExp: "条件表达式",
    ast.ListComp: "列表推导",
    ast.SetComp: "集合推导",
    ast.DictComp: "字典推导",
    ast.GeneratorExp: "生成器表达式",
    ast.Starred: "解包",
    ast.JoinedStr: "f-string",
    ast.Await: "await",
    ast.NamedExpr: "海象运算符",
}


class CalculatorArgs(BaseModel):
    """``calculator`` 参数。"""

    expression: str = Field(
        max_length=EXPRESSION_MAX_CHARS,
        description="算术表达式，例如 (12.5 - 8) / 8 * 100",
    )

    @field_validator("expression")
    @classmethod
    def _check_expression(cls, value: str) -> str:
        """语法 + 白名单校验（在参数校验阶段完成，见模块 docstring）。"""
        text = value.strip()
        if not text:
            msg = "表达式不能为空"
            raise ValueError(msg)
        if len(text) > EXPRESSION_MAX_CHARS:
            msg = f"表达式不得超过 {EXPRESSION_MAX_CHARS} 个字符"
            raise ValueError(msg)
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError as exc:
            msg = f"表达式语法错误：{exc.msg}"
            raise ValueError(msg) from exc
        _assert_whitelisted(tree)
        return text


def _assert_whitelisted(tree: ast.Expression) -> None:
    """逐节点检查白名单；遇到禁用节点即抛 ``ValueError``。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Expression | ast.Constant | ast.BinOp | ast.UnaryOp):
            if isinstance(node, ast.Constant) and not isinstance(node.value, int | float):
                msg = f"只允许数字常量，收到 {type(node.value).__name__}"
                raise ValueError(msg)
            if isinstance(node, ast.BinOp) and type(node.op) not in _BIN_OPS:
                msg = f"不支持的运算符：{type(node.op).__name__}"
                raise ValueError(msg)
            if isinstance(node, ast.UnaryOp) and type(node.op) not in _UNARY_OPS:
                msg = f"不支持的一元运算符：{type(node.op).__name__}"
                raise ValueError(msg)
            continue
        if isinstance(node, ast.operator | ast.unaryop | ast.expr_context | ast.Load):
            # 运算符节点本身（Add/Sub/...）会被 walk 到，属于允许范围
            continue
        label = _NODE_LABELS.get(type(node), type(node).__name__)
        msg = f"表达式含有不允许的语法：{label}"
        raise ValueError(msg)


def evaluate(tree: ast.Expression) -> int | float:
    """递归求值（入参必须已通过 :func:`_assert_whitelisted`）。"""
    node = tree.body
    if isinstance(node, ast.Constant):
        return node.value  # type: ignore[return-value]
    if isinstance(node, ast.BinOp):
        left = evaluate(ast.Expression(node.left))
        right = evaluate(ast.Expression(node.right))
        if isinstance(node.op, ast.Pow) and abs(right) > EXPONENT_MAX:
            # 指数上限：``9**999999`` 会吃满 CPU 与内存
            raise ToolExecutionError(f"指数绝对值不得超过 {EXPONENT_MAX}")
        if isinstance(node.op, ast.Pow) and left == 0 and right < 0:
            raise ToolExecutionError("0 的负数次幂无定义")
        try:
            return _BIN_OPS[type(node.op)](left, right)
        except ZeroDivisionError as exc:
            raise ToolExecutionError("除数为 0") from exc
        except OverflowError as exc:
            raise ToolExecutionError("计算结果溢出") from exc
    if isinstance(node, ast.UnaryOp):
        operand = evaluate(ast.Expression(node.operand))
        return _UNARY_OPS[type(node.op)](operand)
    raise ToolExecutionError("表达式无法求值")  # pragma: no cover - 白名单已排除


class CalculatorTool(BuiltinTool):
    """算术计算工具（纯本地，无外部依赖）。"""

    name = TOOL_NAME
    description = DESCRIPTION
    input_model = CalculatorArgs
    side_effect = "read"
    timeout_seconds = 3.0
    example_arguments: ClassVar[dict[str, Any]] = {"expression": "(12.5 - 8) / 8 * 100"}

    async def run(self, arguments: BaseModel, ctx: ToolContext) -> ToolOutcome:
        args = CalculatorArgs.model_validate(arguments)
        # 参数已校验，这里解析必然成功
        tree = ast.parse(args.expression, mode="eval")
        result = evaluate(tree)
        # 除法的浮点误差（0.1+0.2）会原样传给模型，故把整数值的 float 收敛成 int，
        # 让 "4/2" 得到 2 而不是 2.0 —— 模型对后者的复述更容易出错
        if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
            result = int(result)
        return ToolOutcome(
            status="ok",
            payload={"expression": args.expression, "result": result},
            summary=f"{args.expression} = {_format(result)}",
        )


def _format(value: int | float) -> str:
    """数字格式化：避免 ``0.30000000000000004`` 这类浮点尾巴撑长摘要。"""
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


__all__ = [
    "DESCRIPTION",
    "EXPONENT_MAX",
    "EXPRESSION_MAX_CHARS",
    "TOOL_NAME",
    "CalculatorArgs",
    "CalculatorTool",
    "evaluate",
]
