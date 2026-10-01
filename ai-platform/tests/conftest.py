"""pytest 公共夹具。

三条纪律（docs/11-§2.1）：

1. 每个用例使用**独立资源标识**（如 ``u_test_{uuid}``），互不干扰；
2. 不依赖执行顺序；
3. 配置用 ``_env_file=None`` 构造，**不读开发机上的 .env**，保证结果可复现。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.support.fake_llm import FakeLLM
from tests.support.fake_mcp import FakeMcpServer

from app.agent.loop import AgentLoop
from app.api.deps import PaginationDep, UserId
from app.application.agent import AgentService
from app.application.chat import ChatService
from app.application.context import ContextAssembler
from app.application.memory import MemoryService
from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.security import create_access_token
from app.main import build_agent_services, build_memory_services, create_app
from app.mcp import client as mcp_client_module
from app.mcp.session import McpToolDef
from app.memory.context_store import InMemoryConversationStore
from app.rag.base import NullRetriever, Retriever
from app.rag.embedding.base import EmbeddingProvider
from app.tools import build_tool_service, tool_diagnostics
from app.tools.executor import ToolExecutor

#: 测试基线配置：显式覆盖一切与外部世界有关的项
_BASE: dict[str, Any] = {
    "app_name": "ai-platform",
    "app_env": "local",
    "debug": True,
    "infra_backend": "memory",
    "auth_enabled": True,
    "jwt_secret": "test-secret-0123456789abcdef0123456789abcdef",
    "api_key_pepper": "test-pepper-0123456789abcdef",
    # 用生产默认级别（``INFO``）而不是 ``WARNING``。
    # 教训：``logger.info(..., extra={"created": ...})`` 撞了 ``LogRecord.created``，
    # 但 ``makeRecord`` 只在 ``isEnabledFor`` 为真时才执行 —— 测试里级别是 WARNING，
    # 于是整条 INFO 日志分支从没跑过，「建任务即 500」只在生产才出现。
    # 把测试级别对齐生产默认值，才能让日志调用真的被执行（输出仍被 pytest 捕获，
    # 只有用例失败时才打印）。
    "log_level": "INFO",
    "log_format": "console",
    "openai_api_key": "sk-test-key",
    "llm_model": "fake-flash",
    # ---- RAG（M3）----
    # 三条都是「测试里绝不能走真模型/真网络」的开关：
    # * hash 向量化：零下载、跨进程确定（bge 要下数 GB 权重）；
    # * 关掉 reranker：默认模型名含 bge，开着会去加载 FlagReranker；
    # * inline 任务执行：上传后同步跑完入库，避免用例依赖外部 Worker。
    # 想验证「建了任务但还没入库」（``AC-RAG-04``）时，用
    # ``make_settings(task_runner="none")`` 显式覆盖。
    "embedding_provider": "hash",
    # 维度与模型名也**必须显式钉住**：``_env_file=None`` 只关掉 .env 文件，
    # **关不掉已经存在于进程环境里的同名变量**。而 ``import pymilvus`` 会在
    # import 期调 ``load_dotenv()``，把开发机 .env 灌进 ``os.environ``
    # （见 app/rag/vectorstore/milvus.py::_import_pymilvus_guarded 的说明）。
    # 钉住之后，无论环境里有什么，测试断言的都是"库内默认档"。
    "embedding_model": "BAAI/bge-m3",
    "embedding_dim": 1024,
    "milvus_vector_dim": 1024,
    "reranker_enabled": False,
    "task_runner": "inline",
    # ---- M6 可观测 ----
    # 每个用例都会 ``create_app`` 一次；若沿用默认的固定端口 9100，多个用例
    # 交替启停时会撞上「地址已占用」（``metrics.server_failed ... OSError``）。
    # 端口 0 = 由操作系统分配空闲端口，于是**既不检查端口、又真的走了监听路径**。
    "metrics_port": 0,
    # 不往 localhost:4317 发 OTLP：没有 collector 时后台导出会一直重试并刷日志。
    # tracing 对象仍然构造（``get_tracing()`` 保证非 None），只是不导出。
    "otel_enabled": False,
    "llm_models": [
        {
            "name": "fake-flash",
            "provider": "fake",
            "supports_tools": True,
            "supports_stream": True,
            "context_window": 65536,
        },
        {
            "name": "fake-pro",
            "provider": "fake",
            "supports_tools": True,
            "supports_stream": True,
            "context_window": 131072,
        },
    ],
}


def build_settings(**overrides: Any) -> Settings:
    """构造隔离的测试配置（不读 ``.env``）。"""
    values = {**_BASE, **overrides}
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_process_env() -> Iterator[None]:
    """每个用例前后都把 ``os.environ`` 还原，防止第三方库把 .env 灌进进程环境。

    为什么需要它（实测教训）：``import pymilvus`` 会在 import 期执行
    ``dotenv.load_dotenv()``，把**开发机的 .env** 写进 ``os.environ``；
    ``Settings(_env_file=None, ...)`` 照样会读到（环境变量与 env_file 是两条独立来源）。
    后果是"先跑过任何真实向量库用例之后，后面所有用例的配置都变成开发机的 .env" ——
    失败现象与其原因完全指不到一起（比如"就绪探针为何报 2048 维"）。

    应用侧已在 ``app/rag/vectorstore/milvus.py`` 里做了快照-恢复；这里再加一道
    用例级保险：任何库泄漏的环境变量都不会跨用例传播。
    """
    snapshot = dict(os.environ)
    yield
    for key in set(os.environ) - set(snapshot):
        del os.environ[key]
    for key, value in snapshot.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """返回配置工厂，便于单个用例覆盖字段。"""
    return build_settings


@pytest.fixture
def settings() -> Settings:
    """默认可用的测试配置。"""
    return build_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """装配好的应用实例。"""
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """带 lifespan 的同步测试客户端。"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_token() -> Callable[..., str]:
    """签发 JWT 的便捷方法。"""

    def _make(user_id: str, settings: Settings, **kwargs: Any) -> str:
        return create_access_token(user_id, settings, **kwargs)

    return _make


@pytest.fixture
def auth_headers(settings: Settings, make_token: Callable[..., str]) -> dict[str, str]:
    """一个有效用户的认证头。"""
    return {"Authorization": f"Bearer {make_token('u_test', settings)}"}


@pytest.fixture
def probe_app(
    make_settings: Callable[..., Settings],
) -> Callable[..., tuple[FastAPI, Settings]]:
    """构造一个「带探针路由」的应用，用于契约级断言。

    这里挂几条**只在测试里存在**的路由，专门用来验证错误信封、鉴权、分页与
    500 兜底的行为 —— 业务路由一旦被删改，这些断言仍然有效。
    """

    def _build(**overrides: Any) -> tuple[FastAPI, Settings]:
        settings = make_settings(**overrides)
        application = create_app(settings)
        prefix = settings.api_prefix

        @application.get(f"{prefix}/probe/whoami")
        async def whoami(user_id: UserId) -> dict[str, str]:
            """回显当前用户，用于验证鉴权链路。"""
            return {"user_id": user_id}

        @application.get(f"{prefix}/probe/paginated")
        async def paginated(pagination: PaginationDep) -> dict[str, Any]:
            """回显分页参数，用于验证分页校验与游标解码。"""
            return {
                "limit": pagination.limit,
                "cursor": pagination.cursor,
                "position": None
                if pagination.position is None
                else [pagination.position[0].isoformat(), pagination.position[1]],
            }

        @application.post(f"{prefix}/probe/kb-not-found")
        async def kb_not_found() -> None:
            """抛业务错误，用于验证错误信封。"""
            raise AppError(
                ErrorCode.KB_NOT_FOUND,
                details={"kb_id": "kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"},
            )

        @application.post(f"{prefix}/probe/rate-limited")
        async def rate_limited() -> None:
            """抛可重试错误，用于验证 Retry-After。"""
            raise AppError(ErrorCode.RATE_LIMITED, retry_after=7)

        @application.post(f"{prefix}/probe/crash")
        async def crash() -> None:
            """抛未预期异常，用于验证 500 兜底不泄漏堆栈。"""
            raise RuntimeError("kaboom: internal detail must not leak")

        return application, settings

    return _build


@pytest.fixture
def fake_llm() -> FakeLLM:
    """脚本化 LLM 替身（记录入参 messages，可制造上游故障与取消）。"""
    return FakeLLM()


@pytest.fixture
def make_chat_app(
    make_settings: Callable[..., Settings],
) -> Callable[..., tuple[FastAPI, Settings]]:
    """构造「对话链路接了替身」的应用。

    刻意在 ``create_app`` 之后替换 ``app.state`` 而不是给 ``create_app`` 加参数：
    外部依赖的注入点是既有的 ``app.state``，测试与生产走的是**同一条装配路径**。
    """

    def _build(
        *,
        llm: FakeLLM | None = None,
        retriever: Retriever | None = None,
        **overrides: Any,
    ) -> tuple[FastAPI, Settings]:
        settings = make_settings(**overrides)
        application = create_app(settings)
        service = ChatService(
            settings,
            llm=llm or FakeLLM(),
            store=InMemoryConversationStore(settings),
            retriever=retriever if retriever is not None else NullRetriever(),
            assembler=ContextAssembler(settings),
        )
        application.state.chat_service = service
        application.state.llm = service.llm
        return application, settings

    return _build


@pytest.fixture
def chat_client(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    fake_llm: FakeLLM,
) -> Iterator[TestClient]:
    """默认接 ``fake_llm`` 的对话客户端。"""
    application, _ = make_chat_app(llm=fake_llm)
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture
def chat_headers(settings: Settings, make_token: Callable[..., str]) -> dict[str, str]:
    """对话接口用的认证头。"""
    return {"Authorization": f"Bearer {make_token('u_chat', settings)}"}


@pytest.fixture
def rag_app(make_settings: Callable[..., Settings]) -> tuple[FastAPI, Settings]:
    """**完整装配**的 RAG 应用（内存仓储 + 内存向量库 + hash 向量化 + inline 任务）。

    刻意与生产走同一个 ``create_app``：RAG 的装配顺序（仓储 → 对象存储 → 向量库
    → 向量化 → 重排 → 检索器 → 任务 → 入库）本身就是容易写错的部分，用一套
    「测试专用装配」就测不到了。想注入替身时替换 ``app.state`` 上的实例。
    """
    settings = make_settings()
    return create_app(settings), settings


@pytest.fixture
def rag_client(
    rag_app: tuple[FastAPI, Settings], make_token: Callable[..., str]
) -> Iterator[TestClient]:
    """带默认认证头的 RAG 客户端。

    认证头放在客户端默认值上（而不是每个请求都传）：用例的正文应该读起来是
    「建库 → 上传 → 检索」这套业务动作，鉴权是背景条件。
    需要验证跨用户隔离时，单个请求显式传 ``headers=`` 覆盖即可。
    """
    application, settings = rag_app
    headers = {"Authorization": f"Bearer {make_token('u_rag', settings)}"}
    with TestClient(application, headers=headers) as test_client:
        yield test_client


@pytest.fixture
def other_user_headers(
    rag_app: tuple[FastAPI, Settings], make_token: Callable[..., str]
) -> dict[str, str]:
    """另一个用户的认证头（多租户隔离断言用）。"""
    _, settings = rag_app
    return {"Authorization": f"Bearer {make_token('u_rag_other', settings)}"}


@pytest.fixture
def rag_chat_client(
    rag_app: tuple[FastAPI, Settings],
    fake_llm: FakeLLM,
    make_token: Callable[..., str],
) -> Iterator[TestClient]:
    """把 **应用自己装配的检索器** 接进对话链路的客户端。

    这一步不能省：``Retriever`` 与写入侧（``IngestionService``）必须共享同一个
    向量库实例，所以对话服务必须复用 ``app.state.retriever``，
    不能自己再 ``build_retriever()`` 造一个（那样检索恒为空）。
    """
    application, settings = rag_app
    service = ChatService(
        settings,
        llm=fake_llm,
        store=InMemoryConversationStore(settings),
        retriever=application.state.retriever,
        assembler=ContextAssembler(settings),
    )
    application.state.chat_service = service
    application.state.llm = service.llm
    headers = {"Authorization": f"Bearer {make_token('u_rag_chat', settings)}"}
    with TestClient(application, headers=headers) as test_client:
        yield test_client


@pytest.fixture
def make_rag_client(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> Callable[..., tuple[FastAPI, Settings, TestClient]]:
    """按需覆盖配置的 RAG 客户端工厂。

    有些验收点只能靠「关掉某个组件」来验：
    ``AC-RAG-04`` 要 ``task_runner="none"``（只建任务不执行）、
    ``REQ-RAG-006`` 的降级要一个会失败的检索器、
    限额类用例要调小 ``max_kb_documents``。用同一套 ``_BASE`` 加覆盖，
    保证差异**只有**被显式写出来的那一项。

    返回三元组 ``(app, settings, client)``，调用方自己 ``with client:`` 进生命周期。
    """

    def _build(**overrides: Any) -> tuple[FastAPI, Settings, TestClient]:
        settings = make_settings(**overrides)
        application = create_app(settings)
        headers = {"Authorization": f"Bearer {make_token('u_rag_factory', settings)}"}
        return application, settings, TestClient(application, headers=headers)

    return _build


# ---------------------------------------------------------------------------
# Agent / 工具（M4）
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_app(make_settings: Callable[..., Settings]) -> tuple[FastAPI, Settings]:
    """**完整装配**的 Agent 应用（内存仓储 + 内存向量库 + hash 向量化 + inline 任务）。

    与 ``rag_app`` 同一个 ``create_app``：工具注册表由生产路径构造，所以
    「工具名/Schema 的启动期校验」和「Agent 服务拿到的注册表」都是真的。
    """
    settings = make_settings()
    return create_app(settings), settings


@pytest.fixture
def agent_service(agent_app: tuple[FastAPI, Settings], fake_llm: FakeLLM) -> AgentService:
    """把 **应用自己装配的注册表** 接到脚本化 LLM 上的 Agent 服务。

    重建 ``AgentService``（而不是只换 ``app.state.llm``）是必要的：``AgentLoop``
    在构造时就绑定了 LLM 客户端与执行器，运行期换 ``app.state`` 对已建好的循环无效。
    ``ChatService`` 也一并换成同一个 ``fake_llm`` 与同一个 ``store``，
    否则 ``/chat`` 与 ``/agent/run`` 会看到两份不同的会话历史。
    """
    application, settings = agent_app
    registry = application.state.tool_registry
    executor = ToolExecutor(settings, registry)
    store = application.state.chat_service.store
    service = AgentService(
        settings,
        llm=fake_llm,
        store=store,
        loop=AgentLoop(settings, fake_llm, registry, executor),
        registry=registry,
    )
    application.state.agent_service = service
    application.state.tool_service = build_tool_service(settings, registry, executor=executor)
    application.state.llm = fake_llm
    application.state.chat_service = ChatService(
        settings,
        llm=fake_llm,
        store=store,
        retriever=application.state.retriever,
        assembler=ContextAssembler(settings),
    )
    return service


@pytest.fixture
def agent_client(
    agent_app: tuple[FastAPI, Settings],
    agent_service: AgentService,
    make_token: Callable[..., str],
) -> Iterator[TestClient]:
    """带默认认证头的 Agent 客户端（``agent_service`` 保证 LLM 是替身）。"""
    application, settings = agent_app
    headers = {"Authorization": f"Bearer {make_token('u_agent', settings)}"}
    with TestClient(application, headers=headers) as test_client:
        yield test_client


@pytest.fixture
def agent_headers(
    agent_app: tuple[FastAPI, Settings], make_token: Callable[..., str]
) -> dict[str, str]:
    """``agent_client`` 同款认证头（需要另建客户端时用）。"""
    _, settings = agent_app
    return {"Authorization": f"Bearer {make_token('u_agent', settings)}"}


# ---------------------------------------------------------------------------
# Memory / 摘要（M5）
# ---------------------------------------------------------------------------


@pytest.fixture
def make_memory_client(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> Callable[..., tuple[FastAPI, Settings, FakeLLM]]:
    """构造「记忆链路全部换成替身 LLM」的客户端。

    为什么不能像 ``agent_service`` 那样只换 ``app.state.llm``：记忆抽取器与摘要器
    各自**持有**一个 LLM 客户端（构造时绑定），换 ``app.state`` 对它们无效 ——
    于是 ``memory_extract`` 任务会去打真实上游。所以这里用生产装配函数
    （``build_memory_services`` / ``build_agent_services``）重建一遍，只把 LLM 换成替身。

    两个替身刻意分开（``llm`` 是对话、``memory_llm`` 是抽取/摘要）：三段流程共用
    一个 ``replies`` 队列时，断言「摘要内容」就会依赖「对话用掉了几条回复」，
    这类耦合会让用例的失败信息完全指错方向。

    处理器注册进的是 ``app.state.task_dispatcher``（**运行中的那个**实例）：
    分发器在调用时才查表，所以后补注册对已经建好的 runner 同样生效。
    返回三元组 ``(app, settings, memory_llm)``。

    ``embedding`` 可换成 :class:`tests.support.memory.ScriptedEmbedding`：记忆的
    两个阈值（去重 0.92 / 注入 0.45）都挂在余弦相似度上，用真实向量测它们
    只能靠碰运气命中边界。向量维度必须与 ``settings.embedding_dim`` 一致，
    否则索引会在 ``upsert`` 时报维度不符。
    """

    def _build(
        *,
        llm: FakeLLM | None = None,
        memory_llm: FakeLLM | None = None,
        embedding: EmbeddingProvider | None = None,
        **overrides: Any,
    ) -> tuple[FastAPI, Settings, FakeLLM]:
        settings = make_settings(**overrides)
        application = create_app(settings)
        chat_llm = llm or FakeLLM()
        mem_llm = memory_llm or FakeLLM(replies=["[]"])
        store = application.state.conversation_store
        memory = build_memory_services(
            settings,
            llm=mem_llm,
            store=store,
            embedding=embedding or application.state.embedding,
            tasks=application.state.task_service,
            dispatcher=application.state.task_dispatcher,
            # 处理器在 create_app 里注册过了。重复注册是装配错误（分发器会直接抛），
            # 所以这里只重建服务，再把**已注册的那个**处理器改绑到新服务上。
            register_handlers=False,
        )
        application.state.memory_handlers.bind(memory["service"])
        agent = build_agent_services(
            settings,
            llm=chat_llm,
            store=store,
            retriever=application.state.retriever,
            memory=memory["service"],
        )
        application.state.memory_service = memory["service"]
        application.state.memory_repo = memory["repo"]
        application.state.memory_index = memory["index"]
        application.state.tool_registry = agent["registry"]
        application.state.tool_service = agent["tool_service"]
        application.state.agent_service = agent["agent_service"]
        application.state.tool_diagnostics = tool_diagnostics(agent["registry"])
        application.state.chat_service = ChatService(
            settings,
            llm=chat_llm,
            store=store,
            retriever=application.state.retriever,
            assembler=ContextAssembler(settings),
            memory=memory["service"],
            tasks=application.state.task_service,
            runner=application.state.task_runner,
        )
        application.state.llm = chat_llm
        return application, settings, mem_llm

    return _build


@pytest.fixture
def memory_app(
    make_memory_client: Callable[..., tuple[FastAPI, Settings, FakeLLM]],
) -> tuple[FastAPI, Settings, FakeLLM]:
    """默认的 Memory 应用（对话与记忆各用一个替身 LLM）。"""
    return make_memory_client()


@pytest.fixture
def memory_client(
    memory_app: tuple[FastAPI, Settings, FakeLLM], make_token: Callable[..., str]
) -> Iterator[TestClient]:
    """带默认认证头的 Memory 客户端。"""
    application, settings, _ = memory_app
    headers = {"Authorization": f"Bearer {make_token('u_mem', settings)}"}
    with TestClient(application, headers=headers) as test_client:
        yield test_client


@pytest.fixture
def memory_headers(
    memory_app: tuple[FastAPI, Settings, FakeLLM], make_token: Callable[..., str]
) -> dict[str, str]:
    """``memory_client`` 同款认证头（需要另建客户端时用）。"""
    _, settings, _ = memory_app
    return {"Authorization": f"Bearer {make_token('u_mem', settings)}"}


@pytest.fixture
def memory_service(memory_app: tuple[FastAPI, Settings, FakeLLM]) -> MemoryService:
    """应用里那个 :class:`MemoryService`（单测直接驱动它，省去 HTTP 层）。"""
    application, _, _ = memory_app
    return cast(MemoryService, application.state.memory_service)


# ---------------------------------------------------------------------------
# MCP（M6）
# ---------------------------------------------------------------------------

#: MCP 契约测试默认配置的 Server 名
MCP_SERVER_NAMES = ("fs", "git")


@pytest.fixture
def fake_mcp_server() -> FakeMcpServer:
    """默认的假 MCP Server（两个工具，其中一个是写操作）。"""
    return FakeMcpServer(
        tools=[
            McpToolDef(
                name="read_file",
                description="读文件",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
            McpToolDef(name="write_file", description="写文件"),
        ]
    )


@pytest.fixture
def make_mcp_client(
    make_settings: Callable[..., Settings],
    make_token: Callable[..., str],
    fake_mcp_server: FakeMcpServer,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., tuple[FastAPI, Settings, TestClient]]:
    """构造「MCP 连接全部走假 Server」的应用与客户端。

    连接是在 ``create_app`` **之后**的 ``lifespan`` 里建立的（``manager.startup()``），
    所以注入点选在 ``app.mcp.client.open_session`` —— 生产装配用的就是这个名字，
    替换它等于「换了 transport，其余一行不动」：管理器、客户端、适配器、路由全是真的。

    返回 ``(app, settings, client)``；``create_app`` 的 ``mcp_servers`` 由参数决定，
    因此「多 Server」「allowlist」「required」这些场景都用同一个夹具覆盖。
    """

    def _build(
        *,
        servers: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        server: FakeMcpServer | None = None,
        **overrides: Any,
    ) -> tuple[FastAPI, Settings, TestClient]:
        target = server or fake_mcp_server
        monkeypatch.setattr(mcp_client_module, "open_session", target.factory)
        payload = (
            servers
            if servers is not None
            else {name: {"command": "fake"} for name in MCP_SERVER_NAMES}
        )
        settings = make_settings(mcp_servers=payload, **overrides)
        application = create_app(settings)
        auth = headers or {"Authorization": f"Bearer {make_token('u_mcp', settings)}"}
        return application, settings, TestClient(application, headers=auth)

    return _build


@pytest.fixture
def mcp_app(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> tuple[FastAPI, Settings, TestClient]:
    """默认的 MCP 应用（两个 Server，各两个工具）。"""
    return make_mcp_client()
