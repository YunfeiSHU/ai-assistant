"""全局配置：统一从环境变量与项目根目录的 .env 读取。

所有字段都可以用「同名、不区分大小写」的环境变量覆盖，例如
``APP_ENV=prod``、``MILVUS_URI=http://milvus:19530``。

设计约定（见 docs/10-非功能需求与可观测性.md §7）：

* 本文件是**配置项的唯一事实来源**；新增配置必须同步 ``.env.example`` 与文档表格。
* 配置校验分两层：``Settings`` 内的字段级约束（pydantic），以及
  :meth:`Settings.validate_for_startup` 的**跨字段 / 环境相关**校验
  （启动期调用，失败即拒绝启动）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 项目根目录（``ai-platform/``），用于定位 .env
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: ``/models`` 的默认白名单。刻意做成静态配置表而不做运行期探测：
#: 探测会引入额外延迟与不确定性，而模型能力是部署期已知的。
DEFAULT_LLM_MODELS: list[dict[str, Any]] = [
    {
        "name": "deepseek-flash",
        "provider": "deepseek",
        "supports_tools": True,
        "supports_stream": True,
        "context_window": 65536,
    },
    {
        "name": "deepseek-v4-pro",
        "provider": "deepseek",
        "supports_tools": True,
        "supports_stream": True,
        "context_window": 131072,
    },
]


class ConfigurationError(RuntimeError):
    """配置缺失或不合法导致无法启动。"""


class Settings(BaseSettings):
    """应用配置。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ==================== 应用 ====================
    app_name: str = "ai-platform"
    app_env: Literal["local", "dev", "staging", "prod"] = "local"
    debug: bool = True
    api_prefix: str = "/api/v1"
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["*"]
    #: 基础设施后端：``memory`` = 进程内实现（本地开发 / 测试），
    #: ``real`` = Redis / MySQL / Milvus / MinIO / Kafka（生产 MUST 为 real）。
    infra_backend: Literal["memory", "real"] = "memory"

    # ==================== 鉴权与日志 ====================
    auth_enabled: bool = True
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "ai-assistant"
    jwt_audience: str = "ai-platform"
    #: ``AUTH_ENABLED=false`` 时的兜底用户；生产强制校验，该值不生效。
    debug_user_id: str = "u_dev"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    #: ``user_id`` 写入日志 / 指标前用 HMAC-SHA256 哈希，避免明文外泄。
    api_key_pepper: str = ""
    #: JSON 请求体上限（字节）；multipart 走 ``upload_max_mb``。
    max_json_body_bytes: int = 2 * 1024 * 1024

    # ==================== LLM ====================
    # 走 OpenAI 兼容协议，换供应商只需改 base_url + model，不用改代码：
    #   DeepSeek  : https://api.deepseek.com/v1   -> deepseek-flash / deepseek-v4-pro
    #   Moonshot  : https://api.moonshot.cn/v1
    #   本地 vLLM : http://localhost:8000/v1      （key 随便填）
    openai_api_key: str = ""
    openai_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-flash"
    #: 部分模型不接受该参数，被拒绝时会被上游忽略或报错
    llm_temperature: float = 0.0
    llm_max_concurrency: int = 8
    llm_timeout_seconds: float = 60.0
    llm_first_token_timeout_seconds: float = 30.0
    max_output_tokens: int = 2048
    context_token_budget: int = 8192
    llm_models: list[dict[str, Any]] = Field(default_factory=lambda: list(DEFAULT_LLM_MODELS))
    #: 启动时发一次最小真实调用核对模型名（``docs/03`` §5）。
    #: 默认关闭：生产环境的启动不该依赖外部网络；本地/预发建议打开。
    llm_verify_model_on_startup: bool = False

    # ==================== Embedding ====================
    # 注意：DeepSeek 不提供 embedding 接口，所以向量化必须用本地 BGE 模型
    # （sentence-transformers），不能像 LLM 那样换个 base_url 就走。
    #: ``bge`` = 真实语义向量（需下载权重）；``hash`` = 确定性词法向量。
    #: 后者用于本地/测试：同一个 query 每次都得到同一向量，不依赖网络与模型文件，
    #: 因此「检索链路」能被机械断言（拿语义向量测链路会因为权重变动而随机失败）。
    embedding_provider: Literal["hash", "bge"] = "hash"
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"
    embedding_batch_size: int = 16
    embedding_dim: int = 1024
    embedding_cache_enabled: bool = True

    # ==================== Reranker (BGE) ====================
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_device: str = "cpu"
    reranker_top_n: int = 5
    reranker_enabled: bool = True

    #: 国内下载 HuggingFace 模型加速，如 ``https://hf-mirror.com``
    hf_endpoint: str = ""

    # ==================== Milvus ====================
    milvus_uri: str = "http://localhost:19530"
    milvus_token: str = ""
    milvus_user: str = "root"
    milvus_password: str = "Milvus"
    milvus_db_name: str = "default"
    milvus_collection: str = "ai_platform_chunks"
    milvus_memory_collection: str = "ai_platform_memories"
    milvus_search_ef: int = 64
    #: 集合声明的向量维度。启动期 MUST 与 ``embedding_dim`` 一致（docs/09-§3），
    #: 不一致时拒绝启动而不是写入一批查不出来的向量。
    milvus_vector_dim: int = 1024

    # ==================== 关系库 / 缓存 / 对象存储 / 队列 ====================
    mysql_dsn: str = "mysql+asyncmy://root:20050613@localhost:3306/ai_platform"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "ai-platform"
    minio_secure: bool = False
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_group_id: str = "ai-platform-worker"

    # ==================== RAG / 入库 ====================
    retrieval_top_k: int = 20
    score_threshold: float = 0.0
    rag_context_token_budget: int = 2500
    rag_merge_adjacent_chunks: bool = True
    upload_max_mb: int = 50
    min_doc_chars: int = 50
    max_doc_pages: int = 500
    max_doc_chunks: int = 10000
    max_kb_documents: int = 1000
    max_kb_count: int = 50
    chunk_size: int = 512
    chunk_overlap: int = 64

    # ==================== Agent / Tool / MCP ====================
    agent_max_steps: int = 8
    agent_timeout_seconds: float = 120.0
    tool_timeout_seconds: float = 10.0
    tool_result_max_chars: int = 4000
    tool_http_fetch_enabled: bool = False
    tool_write_allowlist: list[str] = Field(default_factory=lambda: ["memory_save"])
    #: 全局工具黑名单（优先级高于请求级 denied_tools，见 docs/04 §4.3）
    tool_denylist: list[str] = Field(default_factory=list)
    #: 形如 {"server-name": {"command": "python", "args": ["-m", "xxx"]}}，
    #: 固定 Schema 见 docs/05-§2.2
    mcp_servers: dict[str, Any] = {}
    mcp_startup_timeout_seconds: float = 15.0

    # ==================== Memory ====================
    memory_ttl_days: int = 7
    memory_max_messages: int = 200
    memory_recent_turns: int = 10
    history_token_budget: int = 2400
    memory_token_budget: int = 600
    memory_top_n: int = 3
    memory_min_confidence: float = 0.7
    memory_dedupe_threshold: float = 0.92
    memory_score_threshold: float = 0.45
    #: 长期记忆能力的默认开关（用户可用 ``PUT /memory-settings`` 覆盖）
    memory_enabled: bool = True
    #: 对话结束后是否自动抽取长期记忆（关闭后只能靠 ``memory_save`` 主动写入）
    memory_extract_enabled: bool = True
    #: 单条记忆内容长度限制（``docs/07`` §5.1：5..500）
    memory_content_min_chars: int = 5
    memory_content_max_chars: int = 500
    #: 单个用户长期记忆条数上限（防「越用越慢」；超出时丢最旧的低置信条目）
    memory_max_items: int = 500
    #: ``DELETE /memories?all=true`` 后的冷却期：这段时间内不再抽取旧内容（``REQ-MEM-007``）
    memory_clear_cooldown_hours: int = 24
    summary_token_budget: int = 1200
    summary_keep_recent_turns: int = 10
    summary_min_new_messages: int = 20
    summary_trigger_ratio: float = 0.8
    #: 摘要生成开关与防抖窗口（``docs/07`` §3.1：同一会话 5 分钟内最多 1 次）
    summary_enabled: bool = True
    summary_debounce_seconds: int = 300
    #: system 提示词配额（docs/07 §4.1）
    system_prompt_token_budget: int = 1000

    # ==================== 异步任务 / 可观测 ====================
    #: 任务执行方式：``none`` = 只建任务不执行（Worker 未运行 / 测试断言「只建任务」）；
    #: ``inline`` = 进程内后台任务（本地开发）；``kafka`` = 投递到 Kafka 由独立 Worker 消费。
    task_runner: Literal["none", "inline", "kafka"] = "inline"
    worker_concurrency: int = 2
    #: Embedding 类任务的额外并发闸门（``docs/08`` §5.4：默认 1，避免 CPU 打满）
    worker_embed_concurrency: int = 1
    task_timeout_seconds: int = 1800
    task_max_retries: int = 3
    ingest_queue_max: int = 1000
    #: 自动重试退避基数：``base × 4^(attempt-1)``，即 1s / 4s / 16s（``docs/08`` §5.2）
    task_retry_base_seconds: float = 1.0
    #: 退避抖动比例，实际延迟 = delay × uniform(1-j, 1+j)
    task_retry_jitter: float = 0.2
    #: Worker 轮询延迟重试队列的间隔
    task_retry_poll_seconds: float = 1.0
    #: 补偿扫描间隔（``docs/08`` §5.1：每 30s 扫一次滞留在 PENDING 的任务）
    task_compensation_interval_seconds: float = 30.0
    #: 判为「投递失败」的宽限期（任务创建后多久仍未 QUEUED 视为需要补偿重投）
    task_compensation_grace_seconds: float = 60.0
    #: 补偿重投上限；超过则置 FAILED + ``MQ_UNAVAILABLE``
    task_compensation_max_attempts: int = 3
    #: 兜底年龄上限：超过该时长仍卡在 PENDING 的任务一律置 FAILED。
    #: 计数器在进程内存里，重启会清零，所以必须有这条**持久**的兜底 —— 否则
    #: 「补偿重投一直失败 + 每次重启」会让任务永远停在 PENDING。
    task_pending_max_age_seconds: float = 600.0
    #: 优雅退出时等待在飞任务到检查点的最长时间（``docs/08`` §5.4）
    task_shutdown_grace_seconds: float = 30.0
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "ai-platform"
    otel_traces_sampler_arg: float = 0.1
    #: Prometheus 指标总开关（关闭后所有指标调用变成空操作，便于压测对比）
    metrics_enabled: bool = True
    #: ``/metrics`` 监听端口；``0`` = 不起指标服务（由外部 sidecar 抓取时用）
    metrics_port: int = 9100
    metrics_host: str = "0.0.0.0"

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def is_prod(self) -> bool:
        """是否为生产环境。"""
        return self.app_env == "prod"

    @property
    def is_local(self) -> bool:
        """是否为本地环境。"""
        return self.app_env == "local"

    @property
    def auth_required(self) -> bool:
        """是否必须校验 JWT。

        生产环境忽略 ``AUTH_ENABLED``（docs/02 §2.1 第 3 条）：允许关掉鉴权
        是本地开发的便利，绝不能成为线上后门。
        """
        return self.is_prod or self.auth_enabled

    @property
    def debug_tools_enabled(self) -> bool:
        """``POST /tools/{name}/invoke`` 等调试接口是否开放。"""
        return self.app_env in ("local", "dev")

    @property
    def upload_max_bytes(self) -> int:
        """单文件大小上限（字节）。"""
        return self.upload_max_mb * 1024 * 1024

    @property
    def uses_shared_task_store(self) -> bool:
        """任务状态是否存放在**进程外**的共享存储里。

        只有共享存储才能让「API 建任务 → 独立 Worker 执行」成立：内存实现下
        Worker 拿到消息后查不到那行任务。所以它是 ``TASK_RUNNER=kafka`` 的
        前置条件，也是 ``/tasks/{id}/events`` 能跨进程推事件的前提。
        """
        return self.infra_backend == "real"

    @property
    def milvus_connection_args(self) -> dict[str, Any]:
        """传给 PyMilvus / langchain-milvus 的连接参数。"""
        args: dict[str, Any] = {"uri": self.milvus_uri, "db_name": self.milvus_db_name}
        if self.milvus_token:
            args["token"] = self.milvus_token
        elif self.milvus_user:
            args["user"] = self.milvus_user
            args["password"] = self.milvus_password
        return args

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    @field_validator("cors_origins", "tool_write_allowlist", "tool_denylist", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """允许用逗号分隔的字符串代替 JSON 数组（更贴合 .env 书写习惯）。"""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def validate_for_startup(self) -> None:
        """跨字段校验；不通过则抛 :class:`ConfigurationError` 拒绝启动。

        只校验「错了会静默出事故」的项，不做过度校验。
        """
        errors: list[str] = []

        if self.is_prod:
            if not self.auth_required:  # pragma: no cover - 由 auth_required 保证
                errors.append("APP_ENV=prod 时 AUTH_ENABLED 必须为 true")
            if self.infra_backend != "real":
                errors.append("APP_ENV=prod 时 INFRA_BACKEND 必须为 real")
            if "*" in self.cors_origins:
                errors.append("APP_ENV=prod 时 CORS_ORIGINS 不允许为 ['*']")

        if self.auth_required and not self.jwt_secret:
            errors.append("AUTH_ENABLED 开启时 JWT_SECRET 必填（与 Go 侧共享的 HS256 密钥）")

        if not self.is_local and not self.openai_api_key:
            errors.append("非 local 环境必须配置 OPENAI_API_KEY")

        if self.chunk_overlap >= self.chunk_size:
            errors.append(
                f"CHUNK_OVERLAP({self.chunk_overlap}) 必须小于 CHUNK_SIZE({self.chunk_size})"
            )

        if self.context_token_budget <= self.system_prompt_token_budget + self.max_output_tokens:
            errors.append(
                "CONTEXT_TOKEN_BUDGET 必须大于 SYSTEM_PROMPT_TOKEN_BUDGET + MAX_OUTPUT_TOKENS"
            )

        if self.mcp_startup_timeout_seconds <= 0:
            errors.append("MCP_STARTUP_TIMEOUT_SECONDS 必须为正数")

        if self.metrics_port < 0 or self.metrics_port > 65535:
            errors.append(f"METRICS_PORT 必须在 [0,65535]（当前 {self.metrics_port}）")

        if not 0.0 <= self.otel_traces_sampler_arg <= 1.0:
            errors.append(
                f"OTEL_TRACES_SAMPLER_ARG 必须在 [0,1]（当前 {self.otel_traces_sampler_arg}）"
            )

        if self.milvus_search_ef < 1:
            errors.append("MILVUS_SEARCH_EF 必须 ≥ 1")

        if self.embedding_dim != self.milvus_vector_dim:
            # 维度不一致时写入的向量**查不出来也看不出来**（Milvus 只会在对比时报维度错），
            # 所以必须在启动期就拦住（docs/09-§3 / AC-DATA-03）。
            errors.append(
                f"VECTOR_DIM_MISMATCH: EMBEDDING_DIM({self.embedding_dim}) 必须等于 "
                f"MILVUS_VECTOR_DIM({self.milvus_vector_dim})"
            )

        if self.task_runner == "kafka" and not self.kafka_bootstrap_servers:
            errors.append("TASK_RUNNER=kafka 时必须配置 KAFKA_BOOTSTRAP_SERVERS")

        if self.task_retry_base_seconds <= 0:
            errors.append("TASK_RETRY_BASE_SECONDS 必须为正数")

        if not 0.0 <= self.task_retry_jitter < 1.0:
            errors.append(f"TASK_RETRY_JITTER 必须在 [0,1)（当前 {self.task_retry_jitter}）")

        if self.task_compensation_max_attempts < 1:
            errors.append("TASK_COMPENSATION_MAX_ATTEMPTS 必须 ≥ 1")

        if self.worker_concurrency < 1:
            errors.append(f"WORKER_CONCURRENCY 必须 ≥ 1（当前 {self.worker_concurrency}）")

        if self.worker_embed_concurrency < 1:
            errors.append(
                f"WORKER_EMBED_CONCURRENCY 必须 ≥ 1（当前 {self.worker_embed_concurrency}）"
            )

        if self.task_timeout_seconds <= 0:
            errors.append("TASK_TIMEOUT_SECONDS 必须为正数")

        if self.ingest_queue_max < 1:
            errors.append(f"INGEST_QUEUE_MAX 必须 ≥ 1（当前 {self.ingest_queue_max}）")

        if self.task_runner == "kafka" and not self.uses_shared_task_store and self.is_prod:
            # 内存任务表 + 独立进程消费 = Worker 永远查不到任务（消息被 ack 丢弃，
            # 任务永久停在 PENDING）。本地调试允许，生产直接拒绝启动。
            errors.append(
                "TASK_RUNNER=kafka 时 INFRA_BACKEND 必须为 real（任务状态需要跨进程共享）"
            )

        if self.agent_max_steps < 1:
            errors.append(f"AGENT_MAX_STEPS 必须 ≥ 1（当前 {self.agent_max_steps}）")

        if self.agent_timeout_seconds <= 0:
            errors.append("AGENT_TIMEOUT_SECONDS 必须为正数")

        if self.tool_timeout_seconds <= 0:
            errors.append("TOOL_TIMEOUT_SECONDS 必须为正数")

        if self.tool_result_max_chars <= 0:
            errors.append("TOOL_RESULT_MAX_CHARS 必须为正数")

        if self.tool_write_allowlist and self.tool_denylist:
            clash = sorted(set(self.tool_write_allowlist) & set(self.tool_denylist))
            if clash:
                # 同时出现在「写白名单」和「全局黑名单」里 = 配置自相矛盾，
                # 运行期会表现为「某些写工具时有时无」，不如启动即失败。
                errors.append(f"TOOL_WRITE_ALLOWLIST 与 TOOL_DENYLIST 冲突：{clash}")

        if self.memory_top_n < 1:
            errors.append(f"MEMORY_TOP_N 必须 ≥ 1（当前 {self.memory_top_n}）")

        if not 0.0 <= self.memory_min_confidence <= 1.0:
            errors.append(
                f"MEMORY_MIN_CONFIDENCE 必须在 [0,1]（当前 {self.memory_min_confidence}）"
            )

        if not 0.0 <= self.memory_dedupe_threshold <= 1.0:
            errors.append(
                f"MEMORY_DEDUPE_THRESHOLD 必须在 [0,1]（当前 {self.memory_dedupe_threshold}）"
            )

        if not 0.0 <= self.memory_score_threshold <= 1.0:
            errors.append(
                f"MEMORY_SCORE_THRESHOLD 必须在 [0,1]（当前 {self.memory_score_threshold}）"
            )

        if self.memory_content_min_chars < 1:
            errors.append("MEMORY_CONTENT_MIN_CHARS 必须 ≥ 1")

        if self.memory_content_max_chars <= self.memory_content_min_chars:
            errors.append(
                "MEMORY_CONTENT_MAX_CHARS 必须大于 MEMORY_CONTENT_MIN_CHARS"
                f"（当前 {self.memory_content_max_chars} / {self.memory_content_min_chars}）"
            )

        if not 0.0 < self.summary_trigger_ratio <= 1.0:
            errors.append(
                f"SUMMARY_TRIGGER_RATIO 必须在 (0,1]（当前 {self.summary_trigger_ratio}）"
            )

        if self.summary_min_new_messages < 1:
            errors.append("SUMMARY_MIN_NEW_MESSAGES 必须 ≥ 1")

        if self.summary_keep_recent_turns < 0:
            errors.append(
                f"SUMMARY_KEEP_RECENT_TURNS 必须 ≥ 0（当前 {self.summary_keep_recent_turns}）"
            )

        if errors:
            raise ConfigurationError("配置校验失败：\n  - " + "\n  - ".join(errors))


@lru_cache
def get_settings() -> Settings:
    """返回进程内单例配置（便于 FastAPI 依赖注入）。"""
    return Settings()


def apply_hf_endpoint(settings: Settings) -> str | None:
    """让 ``HF_ENDPOINT`` 真正对 ``huggingface_hub`` 生效，返回生效值（未配置则 ``None``）。

    为什么需要这个函数（这是个真踩过的坑，见 docs/12-§13.1）：

    1. ``huggingface_hub`` 只认**进程环境变量**，不读 ``.env``；于是
       「在 .env 里写了 ``HF_ENDPOINT``」看起来配了镜像、实际仍去连
       ``huggingface.co``，表现为「首次加载模型卡 5 次重试 × 超时后才降级」。
    2. 更糟的是它在 **import 期** 就把值固化进模块常量::

           ENDPOINT = os.getenv("HF_ENDPOINT", _HF_DEFAULT_ENDPOINT).rstrip("/")
           HUGGINGFACE_CO_URL_TEMPLATE = ENDPOINT + "/{repo_id}/resolve/{revision}/{filename}"

       ``hf_hub_url()`` 用的是 ``HUGGINGFACE_CO_URL_TEMPLATE``，**只有显式传
       ``endpoint=`` 参数时**才会把 URL 重写成别的域名。所以「import 之后再设
       环境变量」是**完全不生效**的。
    3. 而 ``app`` 自己会在 ``import app.main`` 期间就把 ``huggingface_hub``
       拉进来：``app.rag.chunking`` → ``langchain_text_splitters`` →
       ``transformers`` → ``huggingface_hub``。等 ``create_app()`` 执行时早已
       错过了窗口期 —— 实测 ``os.environ`` 里明明有值，请求仍发往
       ``huggingface.co``。

    因此这里做两件事：写环境变量（对本进程后续才导入的模块、以及子进程有效），
    并且**如果 huggingface_hub 已经被导入，就同步改写它那两个常量**
    （这是唯一能在导入顺序不可控时确定性生效的办法）。
    """
    configured = settings.hf_endpoint.strip()
    existing = os.environ.get("HF_ENDPOINT", "").strip()
    # 真实环境变量（容器编排注入、命令行前缀）优先于 .env，
    # 与 pydantic-settings 的优先级一致。
    endpoint = existing or configured
    if not endpoint:
        return None
    os.environ["HF_ENDPOINT"] = endpoint

    try:  # huggingface_hub 是可选依赖（EMBEDDING_PROVIDER=hash 时不会装）
        from huggingface_hub import constants as hf_constants
    except ImportError:  # pragma: no cover - 取决于可选依赖是否安装
        return endpoint

    if getattr(hf_constants, "ENDPOINT", None) != endpoint:
        hf_constants.ENDPOINT = endpoint
        hf_constants.HUGGINGFACE_CO_URL_TEMPLATE = (
            endpoint + "/{repo_id}/resolve/{revision}/{filename}"
        )
    return endpoint
