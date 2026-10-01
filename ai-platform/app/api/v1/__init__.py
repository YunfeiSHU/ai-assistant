"""路由注册：所有子路由统一挂载到 ``api_router``。

前缀与 tag 都集中在这里声明，避免散落在各路由文件里。

**注册顺序无关紧要，但路径不能互相遮蔽**：``/documents/{doc_id}`` 与
``/knowledge-bases/{kb_id}/documents`` 前缀不同，不会冲突；真正要注意的是
``APIRouter`` 必须保持 ``redirect_slashes=False``（见 ``app/main.py``），
否则 ``GET /knowledge-bases/`` 会被 307 重定向到 ``/knowledge-bases``，
前端拿到的响应语义会变。
"""

from fastapi import APIRouter

from app.api.v1 import (
    agent,
    chat,
    documents,
    health,
    knowledge_bases,
    mcp,
    memory,
    models,
    tasks,
    tools,
)

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(chat.router, prefix="/chat", tags=["对话"])
api_router.include_router(models.router, tags=["对话"])
api_router.include_router(agent.router, tags=["Agent"])
api_router.include_router(tools.router, tags=["工具"])
api_router.include_router(knowledge_bases.router, tags=["知识库"])
api_router.include_router(documents.router, tags=["文档"])
api_router.include_router(tasks.router, tags=["任务"])
api_router.include_router(memory.router, tags=["记忆"])
api_router.include_router(mcp.router, tags=["MCP"])

__all__ = ["api_router"]
