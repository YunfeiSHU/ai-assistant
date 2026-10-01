"""核心横切能力：错误码、ID、日志、鉴权、中间件、健康检查、token 计数。

本包不依赖任何业务模块，业务模块可以放心 import。
"""

from app.core.exceptions import ERROR_SPECS, AppError, ErrorCode, ErrorSpec

__all__ = ["ERROR_SPECS", "AppError", "ErrorCode", "ErrorSpec"]
