"""FastAPI 依赖注入：配置、当前用户、分页参数。

配置一律通过 ``app.state.settings`` 读取，而不是 ``Depends(get_settings)`` —— 后者是
进程级单例的 ``lru_cache``，在同一个进程里创建多个应用实例（测试常态）时会串味。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, cast

from fastapi import Depends, Query, Request

from app.application.agent import AgentService
from app.application.chat import ChatService
from app.application.memory import MemoryService
from app.core.config import Settings
from app.core.pagination import decode_cursor
from app.core.security import AuthUser, authenticate
from app.llm.base import LLMClient
from app.mcp.manager import McpManager
from app.rag.service import (
    DocumentService,
    KnowledgeBaseService,
    SearchService,
)
from app.tasks.events import TaskEventBus
from app.tasks.runner import TaskRunner
from app.tasks.service import TaskService
from app.tools.registry import ToolRegistry
from app.tools.service import ToolService


def get_app_settings(request: Request) -> Settings:
    """取出当前应用实例的配置。"""
    return cast(Settings, request.app.state.settings)


def get_current_user(request: Request) -> AuthUser:
    """校验 JWT 并返回当前用户（未通过则抛 ``401 UNAUTHENTICATED``）。"""
    settings = get_app_settings(request)
    return authenticate(
        authorization=request.headers.get("authorization"),
        settings=settings,
        debug_user_id=request.headers.get("x-debug-user-id"),
    )


def get_user_id(user: Annotated[AuthUser, Depends(get_current_user)]) -> str:
    """只取 ``user_id``（所有存储访问的隔离键）。"""
    return user.user_id


@dataclass(frozen=True, slots=True)
class Pagination:
    """分页参数。"""

    limit: int
    cursor: str | None

    @property
    def position(self) -> tuple[datetime, str] | None:
        """解码后的 ``(created_at, id)`` 起始位置；无游标时为 ``None``。"""
        if not self.cursor:
            return None
        return decode_cursor(self.cursor)


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=100, description="每页条数 1..100")] = 20,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
) -> Pagination:
    """解析分页参数。越界时由统一异常处理器转成 ``400 INVALID_ARGUMENT``。"""
    return Pagination(limit=limit, cursor=cursor or None)


def get_llm_client(request: Request) -> LLMClient:
    """取出当前应用实例的 LLM 客户端（测试可换成脚本化替身）。"""
    return cast(LLMClient, request.app.state.llm)


def get_chat_service(request: Request) -> ChatService:
    """取出对话编排服务。"""
    return cast(ChatService, request.app.state.chat_service)


def get_kb_service(request: Request) -> KnowledgeBaseService:
    """取出知识库服务。"""
    return cast(KnowledgeBaseService, request.app.state.kb_service)


def get_document_service(request: Request) -> DocumentService:
    """取出文档服务。"""
    return cast(DocumentService, request.app.state.document_service)


def get_task_service(request: Request) -> TaskService:
    """取出任务服务。"""
    return cast(TaskService, request.app.state.task_service)


def get_search_service(request: Request) -> SearchService:
    """取出检索调试服务。"""
    return cast(SearchService, request.app.state.search_service)


def get_task_runner(request: Request) -> TaskRunner:
    """取出任务投递器（重试要重新投递，见 ``docs/08`` §4.4）。"""
    return cast(TaskRunner, request.app.state.task_runner)


def get_task_event_bus(request: Request) -> TaskEventBus:
    """取出任务进度事件总线（``GET /tasks/{id}/events`` 的推送源）。"""
    return cast(TaskEventBus, request.app.state.task_event_bus)


def get_tool_registry(request: Request) -> ToolRegistry:
    """取出工具注册表（``GET /tools`` 与调试调用接口用）。"""
    return cast(ToolRegistry, request.app.state.tool_registry)


def get_tool_service(request: Request) -> ToolService:
    """取出工具服务。"""
    return cast(ToolService, request.app.state.tool_service)


def get_agent_service(request: Request) -> AgentService:
    """取出 Agent 编排服务。"""
    return cast(AgentService, request.app.state.agent_service)


def get_memory_service(request: Request) -> MemoryService:
    """取出长期记忆 / 摘要服务（``docs/07`` §5 / §6）。"""
    return cast(MemoryService, request.app.state.memory_service)


def get_mcp_manager(request: Request) -> McpManager:
    """取出 MCP 连接管理器（``docs/05`` §5）。

    ``create_app`` 在 lifespan 之前就挂好了它，所以即使还没执行启动（连接），路由也能
    拿到一个「全部 Server 处于 ``unavailable``」的管理器而不是 500。
    """
    return cast(McpManager, request.app.state.mcp_manager)


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
CurrentUser = Annotated[AuthUser, Depends(get_current_user)]
UserId = Annotated[str, Depends(get_user_id)]
PaginationDep = Annotated[Pagination, Depends(get_pagination)]
LLMClientDep = Annotated[LLMClient, Depends(get_llm_client)]
ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
KBServiceDep = Annotated[KnowledgeBaseService, Depends(get_kb_service)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
TaskEventBusDep = Annotated[TaskEventBus, Depends(get_task_event_bus)]
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
TaskRunnerDep = Annotated[TaskRunner, Depends(get_task_runner)]
ToolRegistryDep = Annotated[ToolRegistry, Depends(get_tool_registry)]
ToolServiceDep = Annotated[ToolService, Depends(get_tool_service)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]
McpManagerDep = Annotated[McpManager, Depends(get_mcp_manager)]

__all__ = [
    "AgentServiceDep",
    "ChatServiceDep",
    "CurrentUser",
    "DocumentServiceDep",
    "KBServiceDep",
    "LLMClientDep",
    "McpManagerDep",
    "MemoryServiceDep",
    "Pagination",
    "PaginationDep",
    "SearchServiceDep",
    "SettingsDep",
    "TaskEventBusDep",
    "TaskRunnerDep",
    "TaskServiceDep",
    "ToolRegistryDep",
    "ToolServiceDep",
    "UserId",
    "get_agent_service",
    "get_app_settings",
    "get_chat_service",
    "get_current_user",
    "get_document_service",
    "get_kb_service",
    "get_llm_client",
    "get_mcp_manager",
    "get_memory_service",
    "get_pagination",
    "get_search_service",
    "get_task_event_bus",
    "get_task_runner",
    "get_task_service",
    "get_tool_registry",
    "get_tool_service",
    "get_user_id",
]
