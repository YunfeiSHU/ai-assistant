"""应用层：编排领域能力（RAG / Agent / MCP / 记忆）实现一条条用例，供接口层调用。

本层只做「编排 + 降级」，不实现具体算法，也不碰框架细节（HTTP / SSE 的渲染
由 :mod:`app.api` 负责，外部依赖的具体实现在 :mod:`app.infrastructure`）。
依赖方向严格单向：``app.api`` → ``app.application`` → 领域包 → ``app.infrastructure``。
"""
