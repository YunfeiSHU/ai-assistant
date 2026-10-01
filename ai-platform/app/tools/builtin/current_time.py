"""``current_time``：当前时间（``docs/04`` §3，P0）。

看起来最简单，但有三处必须做对：

1. **时区**用 ``zoneinfo`` 而不是 ``datetime.now()`` —— 后者给的是进程本地时区，容器里通常是
   UTC，用户问「现在几点」会得到差 8 小时的答案。
2. **非法时区名是参数问题**（``invalid_arguments``），不是执行失败：模型拿到「Asia/Shangai
   拼错了」能自己改，拿到「执行失败」只能放弃。
3. **用真实时钟**：不要缓存结果，同一个会话里连问两次应该得到两个时间。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from app.tools.base import BuiltinTool, ToolContext, ToolOutcome

TOOL_NAME = "current_time"
DESCRIPTION = (
    "获取指定时区的当前日期、时间与星期。"
    "当问题涉及「今天/现在/本周」这类相对时间，或需要把相对表述换算成具体日期时使用；"
    "它不返回历史日期，也不做日期差计算（那部分交给 calculator）。"
)

#: 默认时区（``docs/04`` §3 表）
DEFAULT_TIMEZONE = "Asia/Shanghai"
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


class CurrentTimeArgs(BaseModel):
    """``current_time`` 参数。"""

    timezone: str = Field(
        default=DEFAULT_TIMEZONE,
        max_length=64,
        description="IANA 时区名，例如 Asia/Shanghai、UTC、America/New_York",
    )

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        name = value.strip() or DEFAULT_TIMEZONE
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            # 拼错的时区名是**参数**问题：模型能自己改成正确的名字重试
            msg = f"未知时区：{name}（需要 IANA 名称，如 Asia/Shanghai）"
            raise ValueError(msg) from exc
        return name


class CurrentTimeTool(BuiltinTool):
    """当前时间工具。"""

    name = TOOL_NAME
    description = DESCRIPTION
    input_model = CurrentTimeArgs
    side_effect = "read"
    timeout_seconds = 2.0
    example_arguments: ClassVar[dict[str, Any]] = {"timezone": "Asia/Shanghai"}

    async def run(self, arguments: BaseModel, ctx: ToolContext) -> ToolOutcome:
        args = CurrentTimeArgs.model_validate(arguments)
        zone = ZoneInfo(args.timezone)
        now = datetime.now(UTC).astimezone(zone)
        payload = {
            "timezone": args.timezone,
            # 带偏移量的 ISO 8601：模型据此做跨时区换算时不会丢了偏移
            "iso": now.isoformat(timespec="seconds"),
            "weekday": _WEEKDAYS[now.weekday()],
            "date": now.date().isoformat(),
        }
        return ToolOutcome(
            status="ok",
            payload=payload,
            summary=f"{args.timezone} 当前时间 {now.strftime('%Y-%m-%d %H:%M:%S')}（{payload['weekday']}）",
        )


__all__ = [
    "DEFAULT_TIMEZONE",
    "DESCRIPTION",
    "TOOL_NAME",
    "CurrentTimeArgs",
    "CurrentTimeTool",
]
