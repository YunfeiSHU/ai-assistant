"""FastAPI 应用入口。

本地运行：``uv run uvicorn app.main:app --reload``

装配顺序（``create_app``）刻意固定为：日志（最早，后续所有步骤都有结构化输出）→ 状态与健康
注册表（挂在 ``app.state``，避免全局单例串味）→ CORS → 请求体限制 → 请求上下文（**上下文必须
最外层**，这样 4xx/5xx 响应也带 trace）→ 异常处理器 → 业务路由。

启动期校验放在 ``lifespan`` 而不是 import 期：这样 ``--reload`` 改代码不会因为环境变量缺失而
反复炸掉进程，也让「配置错误」表现为明确的启动失败而非 import 崩溃。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.agent.loop import AgentLoop
from app.api.exception_handlers import register_exception_handlers
from app.api.v1 import api_router
from app.application.agent import AgentService
from app.application.chat import ChatService
from app.application.context import ContextAssembler
from app.application.memory import MemoryService
from app.core.config import Settings, apply_hf_endpoint, get_settings
from app.core.exceptions import AppError
from app.core.health import build_health_registry
from app.core.logging import get_logger, setup_logging
from app.core.middleware import install_middlewares
from app.infrastructure.mysql.db import (
    aclose_all_engines,
    create_engine_from_settings,
    release_engine,
)
from app.infrastructure.observability import (
    CircuitRegistry,
    Metrics,
    MetricsServer,
    configure_metrics,
    configure_tracing,
    setup_tracing,
    shutdown_tracing,
)
from app.infrastructure.storage import build_object_store, build_repositories
from app.llm.base import LLMClient
from app.llm.openai_compat import OpenAICompatLLM
from app.mcp import (
    McpConfigError,
    McpManager,
    build_mcp_manager,
    make_mcp_check,
    sync_mcp_tools,
)
from app.memory import (
    build_conversation_store,
    build_memory_repo,
    build_memory_vector_index,
)
from app.memory.context_store import ConversationStore, InMemoryConversationStore
from app.memory.extractor import MemoryExtractor
from app.memory.preferences import InMemoryMemoryPreferenceStore
from app.memory.summary import SummaryBuilder
from app.memory.tasks import MemoryTaskHandlers
from app.rag.base import Retriever
from app.rag.embedding import build_embedding_provider
from app.rag.embedding.base import EmbeddingProvider
from app.rag.reranker import build_reranker
from app.rag.reranker.base import Reranker
from app.rag.retriever import Retriever as RagRetriever
from app.rag.service import (
    DocumentService,
    IngestionService,
    KnowledgeBaseService,
    SearchService,
)
from app.rag.vectorstore import build_vector_store
from app.rag.vectorstore.base import VectorStore
from app.tasks.compensation import TaskCompensator
from app.tasks.dispatch import TaskDispatcher
from app.tasks.events import Publisher, build_task_event_bus, make_publisher
from app.tasks.models import TaskType
from app.tasks.runner import (
    InlineTaskRunner,
    KafkaTaskRunner,
    TaskRunner,
    build_task_runner,
    require_kafka_runner,
)
from app.tasks.service import TaskService
from app.tasks.store import TaskStore, build_task_store
from app.tasks.transport import TaskProducer, build_task_producer
from app.tools import build_tool_registry, build_tool_service, tool_diagnostics
from app.tools.executor import ToolExecutor

logger = get_logger("app.main")


def build_retriever(
    settings: Settings,
    *,
    embedding: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
    reranker: Reranker | None = None,
) -> RagRetriever:
    """构造检索器（向量召回 + 可选重排）。

    ``INFRA_BACKEND=memory`` 时向量库是进程内的（暴力余弦，精确），``=real`` 时是 Milvus。两者
    走**同一段**检索代码，所以「过滤、阈值、相邻合并、引用编号」这些逻辑在上线前后行为一致。

    三个组件允许外部注入，而且 :func:`build_rag_services` **必须**注入：写入侧与查询侧必须是
    **同一个**向量库实例。各建各的会让内存实现出现「入库成功、检索永远 0 条」—— 没有异常、没有
    日志，只是查不到，这类问题靠读代码很难发现。
    """
    return RagRetriever(
        settings=settings,
        embedding=embedding if embedding is not None else build_embedding_provider(settings),
        vector_store=vector_store if vector_store is not None else build_vector_store(settings),
        reranker=reranker if reranker is not None else build_reranker(settings),
    )


def build_chat_service(
    settings: Settings,
    retriever: Retriever | None = None,
    *,
    llm: LLMClient | None = None,
    store: ConversationStore | None = None,
    memory: MemoryService | None = None,
    tasks: TaskService | None = None,
    runner: TaskRunner | None = None,
) -> ChatService:
    """构造对话服务（LLM / 存储 / 检索器 / 上下文装配器 / 记忆）。

    所有依赖都可注入，而且 :func:`create_app` **必须**注入：``store`` 要与记忆层共用同一个实例
    （否则「刚说过的话在读记忆时看不见」），``memory`` 要能真正检索长期记忆，``tasks`` /
    ``runner`` 要能把记忆抽取与摘要投递出去。
    """
    return ChatService(
        settings,
        llm=llm if llm is not None else OpenAICompatLLM(settings),
        store=store if store is not None else InMemoryConversationStore(settings),
        retriever=retriever if retriever is not None else build_retriever(settings),
        assembler=ContextAssembler(settings),
        memory=memory,
        tasks=tasks,
        runner=runner,
    )


def build_rag_services(
    settings: Settings,
    *,
    task_store: TaskStore | None = None,
    events: Publisher | None = None,
    producer: TaskProducer | None = None,
) -> dict[str, Any]:
    """构造 RAG 全套服务并返回需要挂到 ``app.state`` 的实例。

    装配顺序就是依赖顺序：仓储 → 对象存储 → 向量库 → Embedding → 重排 → 检索器 → 任务 →
    入库（入库需要上面全部）。

    任务处理器走 :class:`~app.tasks.dispatch.TaskDispatcher`：M5 之后同一进程里至少有 3 种任务类型
    （入库 / 摘要 / 记忆抽取），把 ``ingestion.handle`` 直接交给 runner 会让后两种被当成入库执行。
    分发器先建、后注册，因为「记忆服务需要 embedding、而 runner 需要分发器」形成了装配顺序上的环。

    ``task_store`` / ``events`` / ``producer`` 是 M7 的三个接缝，**Worker 进程也调这个函数**：
    ``task_store`` 必须是跨进程共享的**同一份**（否则 Worker 收到的每个任务都查不到）；
    ``events`` 是 SSE 的事件源；``producer`` 复用调用方已建好的 Kafka 连接。
    """
    repos = build_repositories(settings)
    objects = build_object_store(settings)
    vectors = build_vector_store(settings)
    embedding = build_embedding_provider(settings)
    task_service = TaskService(
        task_store if task_store is not None else build_task_store(settings),
        max_retries=settings.task_max_retries,
        events=events,
        queue_max=settings.ingest_queue_max,
    )
    ingestion = IngestionService(settings, repos, objects, embedding, vectors, task_service)
    dispatcher = TaskDispatcher(task_service)
    dispatcher.register_many(
        {
            TaskType.DOCUMENT_INGEST: ingestion.handle,
            TaskType.DOCUMENT_DELETE: ingestion.handle,
        }
    )
    runner: TaskRunner = build_task_runner(
        settings, task_service, dispatcher.handle, producer=producer
    )
    # 检索器必须复用**同一批**实例（见 build_retriever 的说明）
    retriever = build_retriever(settings, embedding=embedding, vector_store=vectors)
    return {
        "repos": repos,
        "objects": objects,
        "vectors": vectors,
        "embedding": embedding,
        "tasks": task_service,
        "runner": runner,
        "dispatcher": dispatcher,
        "retriever": retriever,
        "kb_service": KnowledgeBaseService(settings, repos, objects, vectors, task_service),
        "document_service": DocumentService(settings, repos, objects, task_service, runner),
        "search_service": SearchService(settings, repos, retriever),
        "ingestion": ingestion,
    }


def build_memory_services(
    settings: Settings,
    *,
    llm: Any,
    store: ConversationStore,
    embedding: EmbeddingProvider,
    tasks: TaskService,
    dispatcher: TaskDispatcher,
    assembler: ContextAssembler | None = None,
    register_handlers: bool = True,
) -> dict[str, Any]:
    """构造长期记忆 / 摘要 / 上下文服务（``docs/07``）。

    ``store`` **必须**与 :class:`ChatService` 用的是同一个实例：对话写入的历史与记忆读取的历史
    必须是同一份，否则会出现「刚刚说过的话在读记忆时看不见」这种只在多实例部署下才稳定的怪现象。

    记忆任务处理器在这里注册进分发器（而不是在 :func:`build_rag_services`）：处理器需要
    ``MemoryService``，而服务又需要 embedding。注册点必须只有一处，否则重复注册会被分发器拦下
    并让应用起不来。

    ``register_handlers=False`` 用于「同一进程内再建一套记忆服务」（测试注入替身 LLM /
    向量化）：此时处理器已经注册过了，调用方必须改成 ``app.state.memory_handlers.bind(新服务)``。
    """
    repo = build_memory_repo(settings)
    index = build_memory_vector_index(settings)
    extractor = MemoryExtractor(settings, llm)
    summary_builder = SummaryBuilder(settings, llm, store)
    service = MemoryService(
        settings,
        repo,
        index,
        store,
        embedding,
        assembler or ContextAssembler(settings),
        extractor=extractor,
        summary_builder=summary_builder,
        preferences=InMemoryMemoryPreferenceStore(),
    )
    handlers = MemoryTaskHandlers(tasks, service)
    if register_handlers:
        dispatcher.register_many(
            {
                TaskType.SUMMARY_BUILD: handlers.handle,
                TaskType.MEMORY_EXTRACT: handlers.handle,
            }
        )
    return {
        "repo": repo,
        "index": index,
        "extractor": extractor,
        "summary_builder": summary_builder,
        "service": service,
        "handlers": handlers,
    }


def build_agent_services(
    settings: Settings,
    *,
    llm: Any,
    store: ConversationStore,
    retriever: Retriever,
    memory: MemoryService | None = None,
) -> dict[str, Any]:
    """构造工具层与 Agent 服务。

    工具注册表在这里**构造即校验**：工具名不合规、内置名重复、描述超长、``parameters`` 不是
    ``type=object`` 的 JSON Schema —— 任一不满足就直接抛异常，让应用起不来（``AC-AGENT-07``）。
    带进运行期的表现是「模型偶尔调错工具」，那种问题靠日志几乎定位不到。

    ``llm`` 与 ``store`` 必须是 :class:`ChatService` 里那两个**同一个**实例：``llm`` 共享是为了
    ``GET /models`` 的「当前默认模型」只有一份真相；``store`` 共享是为了 ``/chat`` 与
    ``/agent/run`` 看到同一份会话历史。

    ``memory`` 传入时才会注册 ``memory_save`` / ``memory_search``。
    """
    registry = build_tool_registry(settings, retriever=retriever, memory=memory)
    executor = ToolExecutor(settings, registry)
    loop = AgentLoop(settings, llm, registry, executor)
    return {
        "registry": registry,
        "executor": executor,
        "tool_service": build_tool_service(settings, registry, executor=executor),
        "agent_service": AgentService(
            settings,
            llm=llm,
            store=store,
            loop=loop,
            registry=registry,
        ),
    }


#: 启动自检期望存在的表（``deploy/mysql/001_init_schema.sql``）。
#: 只要这几张缺失就会让大部分接口 503，所以值得在启动期就点名。
EXPECTED_TABLES = (
    "knowledge_base",
    "document",
    "document_chunk",
    "user_memory",
    "task",
)


async def _check_mysql_ready(settings: Settings) -> None:
    """MySQL 可达性 + 建表自检（**只记日志，不影响启动**）。

    三档结论分明，因为它们对应三种完全不同的处置：连不上 → ``error``（容器编排/凭据问题）；
    连上但缺表 → ``error`` + 缺了哪几张（部署漏了执行建表脚本）；都通 → ``info`` + 表数量。

    刻意**不**写成 ``SELECT 1`` 加一句「OK」：那样「连上了但库是空的」会显示为成功，而真正的
    问题（少建表）要等第一次 ``POST /knowledge-bases`` 才暴露，那时它已经是一次线上故障了。
    """
    if settings.infra_backend != "real":
        return  # 内存后端无需连库，日志里出现一条「未构建」反而是噪音
    try:
        engine = create_engine_from_settings(settings)
    except AppError as exc:
        logger.error("app.mysql_unavailable", extra={"error": exc.message})
        return
    try:
        from sqlalchemy import text

        async with engine.connect() as connection:
            rows = await connection.execute(text("SHOW TABLES"))
            tables = {str(row[0]) for row in rows}
        missing = [name for name in EXPECTED_TABLES if name not in tables]
        if missing:
            logger.error(
                "app.mysql_schema_incomplete",
                extra={
                    "missing_tables": missing,
                    "total_tables": len(tables),
                    "hint": "执行 deploy/mysql/001_init_schema.sql",
                },
            )
        else:
            logger.info("app.mysql_ready", extra={"host": _dsn_host(settings.mysql_dsn)})
    except Exception as exc:  # 依赖故障不该阻断启动（与向量库同一口径）
        logger.error(
            "app.mysql_not_ready",
            extra={"error": f"{type(exc).__name__}: {exc}", "host": _dsn_host(settings.mysql_dsn)},
        )
    finally:
        # 自检**不占着连接**：这里归还引用，计数归零时连接池立即关闭。
        await release_engine(engine)


def _dsn_host(dsn: str) -> str:
    """从 DSN 里抠出 ``host:port/db`` ——凭据 MUST NOT 进日志（``REQ-NFR-009``）。"""
    tail = dsn.split("://", 1)[-1]
    tail = tail.rsplit("@", 1)[-1]
    return tail.split("?", 1)[0]


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建并装配 FastAPI 应用。

    Args:
        settings: 显式传入的配置（测试用）；缺省读取进程级单例配置。

    Returns:
        已完成中间件、异常处理器与路由装配的应用实例。
    """
    resolved = settings or get_settings()

    setup_logging(
        level=resolved.log_level,
        fmt=resolved.log_format,
        service=resolved.otel_service_name,
        pepper=resolved.api_key_pepper,
    )

    # 模型下载镜像必须在**任何模型构造之前**同步进 os.environ：huggingface_hub 读的是进程环境
    # 变量，不读 .env。晚一步设置，首次加载就会去连被墙的 huggingface.co，表现成「第一次重排/
    # 向量化要卡十几分钟然后静默降级」（见 docs/12）。
    hf_endpoint = apply_hf_endpoint(resolved)
    if hf_endpoint:
        logger.info("app.hf_endpoint_applied", extra={"hf_endpoint": hf_endpoint})

    # 可观测性先建：后面每一步（RAG 装配、MCP 连接）都会往指标/链路里写东西，而
    # ``get_metrics()`` / ``get_tracing()`` 在未配置时是空操作 —— 那会让「启动阶段的耗时
    # 与失败」全部丢失，而那恰恰是排障最需要的一段。
    metrics = Metrics(enabled=resolved.metrics_enabled, service=resolved.otel_service_name)
    configure_metrics(metrics)
    tracing = setup_tracing(resolved)
    configure_tracing(tracing)
    circuits = CircuitRegistry()

    # MCP：**只构造不连接**（连接在 lifespan 里 await，这样 create_app 保持同步、
    # 测试可以反复调用而不产生子进程）。配置错误也不在这里抛 —— 见 _deferred_config_error。
    mcp_config_error: McpConfigError | None = None
    try:
        mcp = build_mcp_manager(resolved, circuits=circuits, tracing=tracing)
    except McpConfigError as exc:
        # 与「启动期校验」同一口径（REQ-NFR-014）：配置错误 MUST 拒绝启动，但**不能在
        # import 期炸** —— ``app = create_app()`` 在模块导入时执行，在这里抛会让
        # ``uvicorn --reload`` 连重启提示都打不出来。
        mcp = McpManager({}, circuits=circuits, tracing=tracing)
        mcp_config_error = exc
        logger.error(
            "app.mcp_config_invalid",
            extra={"server": exc.server, "field": exc.field, "error": str(exc)},
        )

    # 服务实例在 lifespan **之前**构造：lifespan 里的启动期自检要用到它们，而闭包变量必须在函数
    # 返回前就绑定好。
    #
    # 任务存储 / 事件总线 / 生产者要先于 RAG 装配：它们是任务层的外部依赖，而 ``TaskService``
    # 在 ``build_rag_services`` 里就被建出来了 —— 后挂上去只会变成「部分服务拿到了总线、部分
    # 没拿到」这种最难查的半生效状态。
    task_store = build_task_store(resolved)
    task_bus = build_task_event_bus(resolved)
    task_publisher = make_publisher(task_bus)
    task_producer = build_task_producer(resolved) if resolved.task_runner == "kafka" else None
    rag = build_rag_services(
        resolved,
        task_store=task_store,
        events=task_publisher,
        producer=task_producer,
    )
    # 只有 kafka 执行器才可能是 :class:`KafkaTaskRunner`；先断言一次，下面的补偿扫描与关停都
    # 依赖这个更窄的类型 —— 配置与实现不一致要在启动期就报出来，而不是等到关停时才在
    # ``isinstance`` 分支里静默地什么都不做
    kafka_runner = require_kafka_runner(rag["runner"]) if resolved.task_runner == "kafka" else None
    compensator = (
        TaskCompensator(resolved, rag["tasks"], rag["runner"]) if kafka_runner is not None else None
    )
    # 会话上下文存储只有一个实例：``/chat``、``/agent/run``、记忆层都读它
    store: ConversationStore = build_conversation_store(resolved)
    llm = OpenAICompatLLM(resolved)
    memory = build_memory_services(
        resolved,
        llm=llm,
        store=store,
        embedding=rag["embedding"],
        tasks=rag["tasks"],
        dispatcher=rag["dispatcher"],
    )
    service = build_chat_service(
        resolved,
        retriever=rag["retriever"],
        llm=llm,
        store=store,
        memory=memory["service"],
        tasks=rag["tasks"],
        runner=rag["runner"],
    )
    agent = build_agent_services(
        resolved,
        llm=service.llm,
        store=service.store,
        retriever=rag["retriever"],
        memory=memory["service"],
    )
    verifier = getattr(service.llm, "verify_model", None)
    # 指标端口服务：与 ``application.state.metrics_server`` 是同一个对象，便于测试直接断言
    # 端口是否真的在监听
    metrics_server = MetricsServer(metrics, host=resolved.metrics_host, port=resolved.metrics_port)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """应用生命周期钩子。"""
        # ---- startup ----
        # 配置错误 MUST 拒绝启动（REQ-NFR-014），而不是运行到一半才报错
        resolved.validate_for_startup()
        if mcp_config_error is not None:
            # 延到这里的理由见 create_app 里构造 mcp 处：import 期不能炸
            raise mcp_config_error
        logger.info(
            "app.startup",
            extra={
                "env": resolved.app_env,
                "version": __version__,
                "infra_backend": resolved.infra_backend,
                "auth_required": resolved.auth_required,
                "task_runner": resolved.task_runner,
                "task_store": type(task_store).__name__,
                "task_event_bus": type(task_bus).__name__,
                "embedding_provider": resolved.embedding_provider,
                "tool_count": len(agent["registry"]),
                "task_types": rag["dispatcher"].types,
                "memory_enabled": resolved.memory_enabled,
                "mcp_servers": mcp.names,
            },
        )
        if resolved.llm_verify_model_on_startup and callable(verifier):
            # 只告警不阻断：上游抖动不该让服务起不来（见 OpenAICompatLLM.verify_model）
            await verifier()
        # 向量库建表/索引放在启动期：放在首次写入路径上会让「第一个用户的第一次上传」承担额外
        # 延迟，而且建表失败会伪装成一次普通的入库失败。但**失败不阻断启动**（与上面 LLM 校验
        # 同一口径）：``docs/10`` 把「Milvus 连不上」定义为运行期依赖故障，由 ``/health/ready``
        # 返回 503 表达；若这里直接崩，容器会陷入重启循环，连不依赖向量库的对话接口都不可用。
        try:
            await rag["vectors"].ensure_ready()
        except Exception as exc:  # 任何启动期探测失败都不应阻断
            logger.warning(
                "app.vector_store_not_ready",
                extra={"error": str(exc), "infra_backend": resolved.infra_backend},
            )
        # 记忆向量索引同理（当前是进程内实现，不会失败；换成 Milvus 后才是真探测）
        try:
            await memory["index"].ensure_ready()
        except Exception as exc:
            logger.warning(
                "app.memory_index_not_ready",
                extra={"error": str(exc), "infra_backend": resolved.infra_backend},
            )
        # MySQL 可达性 + 建表自检，与上面两个 ``ensure_ready`` 同一策略：**只告警不阻断**。
        # 建引擎是同步且惰性的（不连网），所以「构造成功」不等于「连得上」；而**表不存在**是
        # 最常见的部署遗漏，启动期发现能把「部署问题」与「服务端 bug」区分开。
        await _check_mysql_ready(resolved)
        # MCP 连接：``required=true`` 的 Server 连不上会在这里抛，从而**拒绝启动**
        # （AC-MCP-02）；非必需的失败只降级（AC-MCP-01）。
        await mcp.startup()
        # 工具必须在连接成功之后注册：工具的 schema 来自 Server 的 ``tools/list``，而 Agent 与
        # ``/tools`` 都读同一张注册表 —— 所以这里是「连接 → 注册」的固定顺序。
        registered = sync_mcp_tools(agent["registry"], mcp, pepper=resolved.api_key_pepper)
        # 诊断信息在 MCP 工具注册后重算，否则 ``/health/detail`` 看不到它们
        application.state.tool_diagnostics = tool_diagnostics(agent["registry"])
        # 健康检查也要在连接之后注册：它读的是各 Server 的实时状态
        application.state.health_registry.register("mcp", make_mcp_check(mcp))
        # 开关只有 ``METRICS_ENABLED`` 一个：``METRICS_PORT=0`` 的含义是「端口交给操作系统分
        # 配」（多实例部署与测试用得上），所以**不能**把 ``port > 0`` 当成开关 —— 那会把随机
        # 端口误判成关闭。
        if resolved.metrics_enabled:
            started_server = await metrics_server.start()
            application.state.metrics_server_ready = started_server
            # 打实际端口：配成 0 时不打出来就没人知道去哪儿抓
            logger.info(
                "app.metrics_ready",
                extra={
                    "host": resolved.metrics_host,
                    "port": metrics_server.port if started_server else 0,
                },
            )
        logger.info(
            "app.mcp_ready",
            extra={"servers": len(mcp.names), "mcp_tools": len(registered)},
        )
        # 补偿扫描：只由 API 进程跑（Worker 不该扫 —— 它不建任务）。解决「创建了 PENDING 但投递
        # 失败」：没有扫描，这类任务会永远停在 PENDING，客户端一直等一个永远不会开始的任务。
        compensator_stop = asyncio.Event()
        compensator_job: asyncio.Task | None = None
        if compensator is not None:
            compensator_job = asyncio.create_task(
                compensator.run_forever(compensator_stop), name="task-compensator"
            )
        try:
            yield
        finally:
            # ---- shutdown ----
            # 顺序与启动相反：先停对外接口（指标端口），再关上游连接，最后收尾任务。反过来的话，
            # 「正在被采集的进程突然少了连接」会先污染一轮指标。
            await metrics_server.stop()
            if compensator_job is not None:
                compensator_stop.set()
                try:
                    await compensator_job
                except Exception as exc:  # 关停失败不该阻断退出流程
                    logger.warning("app.compensator_stop_failed", extra={"error": str(exc)})
            # MCP 子进程必须显式回收（AC-NFR-12）：stdio 模式下的 Server 是我们的子进程，进程
            # 退出不会自动带走它们 —— ``--reload`` 反复重启会攒下一堆僵死的 python/npx 进程。
            try:
                await mcp.shutdown()
            except Exception as exc:  # 关停失败不该阻断退出流程
                logger.warning("app.mcp_shutdown_failed", extra={"error": str(exc)})
            # 等进程内后台任务收尾，否则「入库到一半就被 SIGTERM 截断」会留下状态为 RUNNING
            # 的任务与半写的数据
            runner = rag["runner"]
            if isinstance(runner, InlineTaskRunner):
                await runner.shutdown()
            # Kafka 生产者是**常驻连接**，不显式关会让 uvicorn --reload 攒下一堆挂着会话的
            # broker 连接（broker 侧要等到会话超时才释放）
            if isinstance(runner, KafkaTaskRunner):
                await runner.close()
            # 关停事件总线与生产者：先断推送（订阅者已经没有消费者了），再断投递。顺序反了会得到
            # 一批「往空总线发消息」的无效 RPC。
            for closer in (task_bus.close, None if task_producer is None else task_producer.stop):
                if closer is None:
                    continue
                try:
                    await closer()
                except Exception as exc:
                    logger.warning("app.task_shutdown_failed", extra={"error": str(exc)})
            # 数据库连接池是**进程内共享且长驻**的（见 mysql/db.py 的引用计数）：不在这里断开，
            # ``--reload`` 每次重启都会给 MySQL 留一批半开连接，而 ``max_connections`` 只有 151。
            # 放在所有业务组件之后：前面几个 closer 里的失败重试还要用连接。
            try:
                await aclose_all_engines()
            except Exception as exc:
                logger.warning("app.engine_shutdown_failed", extra={"error": str(exc)})
            # 未刷出的 span 会静默丢失，必须显式 shutdown
            shutdown_tracing()
            logger.info("app.shutdown")

    application = FastAPI(
        title=resolved.app_name,
        version=__version__,
        description="AI 平台服务：LLM 编排 + RAG 检索增强 + Agent + Memory + MCP",
        # 刻意恒为 False：FastAPI 的 debug 页面会把堆栈直接返回给客户端，与 REQ-NFR-009
        # 「错误响应 MUST NOT 含堆栈」直接冲突。settings.debug 仍然控制日志级别与 --reload。
        debug=False,
        lifespan=lifespan,
    )

    # 状态挂在实例上（而不是模块级单例），便于一个进程内跑多个应用（测试常态）
    application.state.settings = resolved
    application.state.health_registry = build_health_registry(resolved)
    application.state.chat_service = service
    application.state.conversation_store = store
    application.state.memory_service = memory["service"]
    application.state.memory_repo = memory["repo"]
    application.state.memory_index = memory["index"]
    application.state.memory_handlers = memory["handlers"]
    application.state.embedding = rag["embedding"]
    # 分发器也要挂出来：测试要往同一个进程里补注册处理器时，必须注册到**运行中的这个**实例上
    application.state.task_dispatcher = rag["dispatcher"]
    application.state.kb_service = rag["kb_service"]
    application.state.document_service = rag["document_service"]
    application.state.search_service = rag["search_service"]
    application.state.task_service = rag["tasks"]
    application.state.task_runner = rag["runner"]
    # SSE 进度的事件源；连 ``inline`` 模式也要挂（路由统一读它，不能靠 issubclass 判断「这种
    # 模式下没有总线」）
    application.state.task_event_bus = task_bus
    application.state.task_store = task_store
    application.state.ingestion = rag["ingestion"]
    # 检索器也挂上：它必须与入库侧共享同一个向量库实例，任何地方再 ``build_retriever()``
    # 一次都会拿到一个空的库
    application.state.retriever = rag["retriever"]
    application.state.vector_store = rag["vectors"]
    application.state.repositories = rag["repos"]
    # `GET /models` 直接读 LLM 客户端的白名单表；挂同一个实例，避免路由层再 new 一个
    application.state.llm = service.llm
    application.state.tool_registry = agent["registry"]
    application.state.tool_service = agent["tool_service"]
    application.state.agent_service = agent["agent_service"]
    application.state.tool_diagnostics = tool_diagnostics(agent["registry"])
    # 可观测性实例挂出来：路由与测试都要能拿到**这个应用自己的**那一份（模块级 ``get_metrics()``
    # 只记得最后创建的应用，一个进程多实例时会串味）
    application.state.metrics = metrics
    application.state.tracing = tracing
    application.state.circuit_registry = circuits
    application.state.metrics_server = metrics_server
    # MCP 管理器：路由读实时状态，lifespan 负责连接与回收
    application.state.mcp_manager = mcp

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "X-Trace-Id"],
    )
    install_middlewares(
        application,
        max_json_body_bytes=resolved.max_json_body_bytes,
        service=resolved.otel_service_name,
        pepper=resolved.api_key_pepper,
        metrics=metrics,
        tracing=tracing,
    )

    register_exception_handlers(application)
    application.include_router(api_router, prefix=resolved.api_prefix)

    return application


app = create_app()


if __name__ == "__main__":  # pragma: no cover - 手工启动入口
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=_settings.host,
        port=_settings.port,
        reload=_settings.debug,
    )
