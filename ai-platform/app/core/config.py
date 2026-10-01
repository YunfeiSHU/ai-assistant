"""全局配置：统一从环境变量与项目根目录的 .env 读取（约定见 ``docs/10`` §7）。

所有字段都可以用「同名、不区分大小写」的环境变量覆盖（如 ``APP_ENV=prod``）。
本文件是配置项的唯一事实来源：新增配置必须同步 ``.env.example`` 与文档表格。

校验分两层：``Settings`` 内的字段级约束（pydantic），以及
:meth:`Settings.validate_for_startup` 的跨字段 / 环境相关校验（启动期调用，失败即拒绝启动）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 项目根目录（``ai-platform/``），用于定位 .env。
#: ``__file__`` 为 ``app/core/config.py``，因此需要向上一共三层。
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

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

#: 已知 BGE / 云端模型的真实输出维度（来自各自 ``config.json`` 的 ``hidden_size``）。
#:
#: 换模型时 ``EMBEDDING_DIM`` 与 ``MILVUS_VECTOR_DIM`` 必须一起改 —— 只改模型不改维度，
#: 写入的向量查不出来也不报错。启动期的 ``VECTOR_DIM_MISMATCH`` 只保证这两个配置项彼此一致，
#: 不知道模型的真实维度，所以这张表补上「模型 → 维度」这一环。
#:
#: =====================================  ======  ==================================
#: 模型                                    维度    说明
#: =====================================  ======  ==================================
#: ``BAAI/bge-m3``                       1024    当前默认：多语言 + 长上下文
#: ``BAAI/bge-large-zh-v1.5``            1024    中文，与默认同维度（换它不用重建集合）
#: ``BAAI/bge-base-zh-v1.5``              768    中文，体积/质量折中
#: ``BAAI/bge-small-zh-v1.5``             512    中文，最快（换它必须重建集合）
#: =====================================  ======  ==================================
EMBEDDING_MODEL_DIMS: dict[str, int] = {
    "BAAI/bge-m3": 1024,
    "BAAI/bge-large-zh-v1.5": 1024,
    "BAAI/bge-base-zh-v1.5": 768,
    "BAAI/bge-small-zh-v1.5": 512,
    # 火山方舟多模态向量化：维度**随版本变**（实测 2026-10-02）
    "doubao-embedding-vision-251215": 2048,
    "doubao-embedding-vision-250615": 1024,
    # 硅基流动 Qwen3-Embedding 系列：**原生**维度（实测 2026-10-02，均可用 dimensions 降维）
    "Qwen/Qwen3-Embedding-0.6B": 1024,
    "Qwen/Qwen3-Embedding-4B": 2560,
    "Qwen/Qwen3-Embedding-8B": 4096,
}

#: 硅基流动 Qwen3-Embedding 系列允许的降维档位（官方文档明示，仅 Qwen3 支持）。
#: 设 ``SILICONFLOW_EMBEDDING_DIMENSIONS`` 时必须是其中之一；表外的模型一律放行
#: （真实维度由 provider 在第一次编码后自检）。
SILICONFLOW_QWEN3_DIMENSIONS: dict[str, tuple[int, ...]] = {
    "Qwen/Qwen3-Embedding-8B": (64, 128, 256, 512, 768, 1024, 1536, 2048, 2560, 4096),
    "Qwen/Qwen3-Embedding-4B": (64, 128, 256, 512, 768, 1024, 1536, 2048, 2560),
    "Qwen/Qwen3-Embedding-0.6B": (64, 128, 256, 512, 768, 1024),
}


def default_torch_num_threads() -> int:
    """本地 CPU 推理的 ``TORCH_NUM_THREADS`` 默认值：逻辑核数的一半（至少 1）。

    ``0`` 仍是合法的显式取值（= 不干预，保留 torch 默认），但不再作为默认：torch 默认按
    逻辑核数开线程，8MB 文档的向量化会把整机核吃满，同机的 MySQL / Redis / 网关 / 验收脚本
    一起被拖慢 2~3 倍（实测连跑两遍全套：M1 41s→75s、M2 71s→186s，看起来像「产品变慢了」）。

    取一半是「单次向量化略慢」与「整机不被饿死」之间的折中：不做成 2 是因为大文档的绝对耗时
    也会跟着翻倍，而验收与用户感知的正是绝对耗时。本机实测（``ai-platform-go/docs/11-§3.4``）
    这一半核是双赢：``=10`` 比 ``=20`` 的入库快 25~30%，在线请求 ``/health`` 中位
    66.2 → 39.1ms。见 ``app/rag/torch_threads.py``。

    ``os.cpu_count()`` 是逻辑核数（含超线程），也是 torch 自己的口径；取不到时按 2 兜底
    （宁可设 1 也不要退回「不干预」）。
    """
    return max(1, (os.cpu_count() or 2) // 2)


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
    #: 服务间调用凭据（Go 网关的后台任务用，如摘要重建）。与用户 JWT 分开：后台任务
    #: 没有「当前用户」，用用户令牌凑合会让审计日志出现「某个普通用户发起了全局重建」。
    internal_service_token: str = ""

    # ==================== gRPC（Go 网关 → 本地编排）====================
    # 与 HTTP 并存：外部接口走 HTTP/SSE，``Chat`` / ``ChatStream`` 走 gRPC（见 docs/04 §2）。
    grpc_enabled: bool = False
    #: 默认只听回环：开发机上「忘了配鉴权就暴露到 0.0.0.0」是最常见的事故来源，
    #: 容器里部署时显式改成 0.0.0.0。
    grpc_host: str = "127.0.0.1"
    grpc_port: int = 50051
    #: 同时在处理的 gRPC 请求数上限。LLM 调用是长尾 IO，给得比 HTTP 小一些，
    #: 让排队发生在这里（可观测）而不是在下游供应商那里（不可观测）。
    grpc_max_concurrency: int = 64

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
    #: ``bge`` = 真实语义向量（本地权重，需下载）；``hash`` = 确定性词法向量；
    #: ``ark`` = 火山方舟云端多模态向量化；``siliconflow`` = 硅基流动云端（真批量 N 进 N 出）。
    #: ``hash`` 用于本地/测试：同一个 query 每次都得到同一向量，不依赖网络与模型文件，
    #: 因此「检索链路」能被机械断言（拿语义向量测链路会因为权重变动而随机失败）。
    embedding_provider: Literal["hash", "bge", "ark", "siliconflow"] = "hash"
    #: 模型标识：本地是 HF 仓库名，云端是服务商的模型 ID（写入 KB / chunk 元数据）
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"
    #: 调用粒度（不是模型批量）：``bge`` 档是 ``encode(batch_size=...)``；``ark`` 档决定
    #: 每次 ``embed()`` 提交多少条（每条一次 HTTP），建议 32~64；``siliconflow`` 档是
    #: 每次 HTTP 里放几条（端点支持真批量），实测 32 最优。
    embedding_batch_size: int = 16
    #: 入库时同时在飞的批次数（``_embed_and_upsert`` 按窗口 ``asyncio.gather``）。
    #: 云端 provider 下这是「每次调用一次网络往返」之上拿吞吐的唯一办法：实测
    #: （8MB / 5,821 片 / 硅基流动，batch=32）串行 98.2s → 窗口 4/8 分别 40.4s / 40.9s
    #: （窗口 8 已是拐点）。默认 4 留余量；本地 BGE 档建议设 1（多线程 encode 会抢同一批核）。
    ingest_embed_window: int = 4
    embedding_dim: int = 1024
    embedding_cache_enabled: bool = True

    # ==================== 云端 Embedding（火山方舟 Ark）====================
    #: 密钥：只允许放 .env / Secret，禁止提交（``.env.example`` 只放占位符）。
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    #: 单次 ``embed()`` 内的最大并发请求数。实测（490 token 的块、``-251215``）：
    #: 并发 1 → 4.4 req/s、8 → 31.6、32 → 98.9、64 → 99.5（无增益）。默认 16 = 峰值的 60~80%，
    #: 给账户 TPM 留余量。
    ark_embedding_concurrency: int = 16
    ark_embedding_timeout_seconds: float = 30.0
    #: 单条请求的退避重试次数（限流/5xx 才重试；4xx 立刻失败）
    ark_embedding_max_retries: int = 2
    #: 响应里向量的编码：``base64``（float32 小端，响应体约为 float 的 1/3，实测
    #: 34,240B → 11,219B）| ``float``（JSON 浮点数组，可读性好，调试时用）
    ark_embedding_encoding: Literal["base64", "float"] = "base64"
    #: 降维输出（Matryoshka 截断式，实测与全维前 N 维余弦 0.9992）。``0`` = 用模型原生
    #: 维度（推荐：召回最好）；设成 N 时 MUST 与 EMBEDDING_DIM 一致。
    ark_embedding_dimensions: int = 0
    #: 检索指令前缀（如"为检索任务生成向量"），留空则不发该参数
    ark_embedding_instructions: str = ""

    # ==================== 云端 Embedding / Rerank（硅基流动 SiliconFlow）====================
    #: 密钥：只允许放 .env / Secret，禁止提交。同一个 key 同时用于 ``/v1/embeddings``
    #: 与 ``/v1/rerank``。
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    #: 单次 ``embed()`` 内的并发请求数（每请求 ``EMBEDDING_BATCH_SIZE`` 条）。实测
    #: （``Qwen/Qwen3-Embedding-0.6B``，490 token 的块）：batch=32/并发 8 → 379.6 片/s，
    #: batch=48/并发 8 → 289（尾延迟变差）。默认 4 留余量，单机独占可提到 8。
    siliconflow_embedding_concurrency: int = 4
    #: 单次请求超时。它是批量请求：batch=32 时实测中位 612ms、最大 1085ms，
    #: 但上游排队时会显著变长，故给足 60s。
    siliconflow_embedding_timeout_seconds: float = 60.0
    #: 单次请求的退避重试次数（只对 429/5xx/网络重试；4xx 立刻失败）
    siliconflow_embedding_max_retries: int = 2
    #: ``base64`` = float32 小端（单条 21,891B → 5,641B，3.9×）| ``float`` = JSON 数组
    siliconflow_embedding_encoding: Literal["base64", "float"] = "base64"
    #: 降维输出（MRL）。``0`` = 用模型原生维度（推荐）。Qwen3 系列可选维度见
    #: ``SILICONFLOW_QWEN3_DIMENSIONS``；设成 N 时 MUST 与 ``EMBEDDING_DIM`` 一致。
    siliconflow_embedding_dimensions: int = 0
    #: 单次 rerank 请求最多带多少条候选（分片阈值）。文档未给上限，实测 201 条仍 200，
    #: 但耗时 2903ms；为了控住尾延迟默认 100。
    siliconflow_rerank_max_documents: int = 100
    siliconflow_rerank_timeout_seconds: float = 30.0
    siliconflow_rerank_max_retries: int = 1
    #: 重排指令（仅 Qwen3-Reranker 系列支持），留空则不发
    siliconflow_rerank_instruction: str = ""

    # ==================== 本地推理运行时 ====================
    #: 本地 CPU 推理的 torch 线程数上限，只对 `bge` 档生效（bge embedding 与 bge reranker
    #: 是仅有的两个本地推理实现）。当前出厂档 embedding 与 rerank 都走云端 API，进程里不跑
    #: torch，因此这一项不影响任何东西；切回本地档才重新有意义。
    #: 默认 = 逻辑核数的一半（见 :func:`default_torch_num_threads`）；显式写 ``0`` = 不干预。
    torch_num_threads: int = Field(default_factory=default_torch_num_threads)

    # ==================== Reranker ====================
    #: 重排实现：``bge`` = 本地交叉编码器（需 torch + 权重）；``siliconflow`` = 云端 ``/v1/rerank``。
    #: 用显式枚举而不是「看模型名里有没有 bge」：靠模型名猜会让换模型/接外部 API 时静默退化成
    #: 「不重排」，而「静默不生效」是本项目踩得最多的一类坑（``docs/12-§3``）。
    reranker_provider: Literal["bge", "siliconflow"] = "bge"
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
    #: 计数器在进程内存里、重启会清零，所以必须有这条持久的兜底 —— 否则
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

        生产环境忽略 ``AUTH_ENABLED``（docs/02 §2.1 第 3 条）：允许关掉鉴权是本地开发的
        便利，绝不能成为线上后门。
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
        """任务状态是否存放在进程外的共享存储里。

        只有共享存储才能让「API 建任务 → 独立 Worker 执行」成立：内存实现下 Worker
        拿到消息后查不到那行任务。所以它是 ``TASK_RUNNER=kafka`` 的前置条件，
        也是 ``/tasks/{id}/events`` 能跨进程推事件的前提。
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

        # 无鉴权的 gRPC 一旦绑到非回环地址，等于把一个可写库、可读知识库的接口开到
        # 内网上；这种配置错误必须在启动期暴露，而不是等到被扫到。
        if self.grpc_enabled and not self.auth_required and not _is_loopback(self.grpc_host):
            errors.append(
                f"GRPC_HOST={self.grpc_host} 非回环地址时 AUTH_ENABLED 必须为 true"
                "（否则任何能连上该端口的人都能以 DEBUG_USER_ID 的身份读写数据）"
            )

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

        if self.torch_num_threads < 0:
            errors.append(
                f"TORCH_NUM_THREADS 必须 ≥ 0（0 = 不干预 / 保留 torch 默认，当前 "
                f"{self.torch_num_threads}）"
            )

        if self.embedding_dim != self.milvus_vector_dim:
            # 维度不一致时写入的向量**查不出来也看不出来**（Milvus 只会在对比时报维度错），
            # 所以必须在启动期就拦住（docs/09-§3 / AC-DATA-03）。
            errors.append(
                f"VECTOR_DIM_MISMATCH: EMBEDDING_DIM({self.embedding_dim}) 必须等于 "
                f"MILVUS_VECTOR_DIM({self.milvus_vector_dim})"
            )

        known_dim = EMBEDDING_MODEL_DIMS.get(self.embedding_model)
        if (
            self.embedding_provider in ("bge", "ark", "siliconflow")
            and known_dim is not None
            and self.embedding_dim != known_dim
        ):
            # 「换了模型没换维度」是最容易踩的一步：只改 EMBEDDING_MODEL 时两个维度配置项
            # 仍然彼此一致，上一条校验不会响，而真实向量维度已经变了 ⇒ 写入成功但永远检索不到。
            # 只在已知模型上判断：自训 / 本地路径 / 表外的云模型一律放行
            # （真实维度由 provider 在第一次编码后自检，见 ark.py::_check_dim）。
            errors.append(
                f"EMBEDDING_MODEL({self.embedding_model}) 的真实输出维度是 {known_dim}，"
                f"但 EMBEDDING_DIM={self.embedding_dim}。换模型必须同时改 "
                f"EMBEDDING_DIM 与 MILVUS_VECTOR_DIM，并**重建 Milvus 集合**"
                f"（已有向量是旧维度，检索不出来）"
            )

        if self.embedding_provider == "ark":
            if not self.ark_api_key:
                errors.append(
                    "EMBEDDING_PROVIDER=ark 时必须配置 ARK_API_KEY"
                    "（密钥只放 .env / Secret，不要写进 .env.example）"
                )
            if not self.ark_base_url.startswith("http"):
                errors.append(f"ARK_BASE_URL 必须是完整 URL（当前 {self.ark_base_url}）")
            if not 1 <= self.ark_embedding_concurrency <= 64:
                errors.append(
                    f"ARK_EMBEDDING_CONCURRENCY 必须在 [1,64]"
                    f"（当前 {self.ark_embedding_concurrency}；实测 32 ≈ 99 req/s，64 无增益）"
                )
            if self.ark_embedding_timeout_seconds <= 0:
                errors.append("ARK_EMBEDDING_TIMEOUT_SECONDS 必须为正数")
            if self.ark_embedding_max_retries < 0:
                errors.append("ARK_EMBEDDING_MAX_RETRIES 必须 ≥ 0")
            if self.embedding_dim <= 0:
                errors.append("EMBEDDING_PROVIDER=ark 时 EMBEDDING_DIM 必须为正整数")
            if self.ark_embedding_dimensions and (
                self.ark_embedding_dimensions != self.embedding_dim
            ):
                # 请求降维到 N，却把 EMBEDDING_DIM 写成别的值 ⇒ 维度自检会在第一次
                # 编码时炸（或更糟：写进维度不符的集合）。启动期就说清楚。
                errors.append(
                    f"ARK_EMBEDDING_DIMENSIONS({self.ark_embedding_dimensions}) 必须等于 "
                    f"EMBEDDING_DIM({self.embedding_dim})；用原生维度请把它设为 0"
                )

        if self.embedding_provider == "siliconflow":
            if not self.siliconflow_api_key:
                errors.append(
                    "EMBEDDING_PROVIDER=siliconflow 时必须配置 SILICONFLOW_API_KEY"
                    "（密钥只放 .env / Secret，不要写进 .env.example）"
                )
            if not self.siliconflow_base_url.startswith("http"):
                errors.append(
                    f"SILICONFLOW_BASE_URL 必须是完整 URL（当前 {self.siliconflow_base_url}）"
                )
            if not 1 <= self.siliconflow_embedding_concurrency <= 32:
                errors.append(
                    f"SILICONFLOW_EMBEDDING_CONCURRENCY 必须在 [1,32]"
                    f"（当前 {self.siliconflow_embedding_concurrency}；"
                    f"实测 batch=32 时并发 8 ≈ 380 片/s）"
                )
            if self.siliconflow_embedding_timeout_seconds <= 0:
                errors.append("SILICONFLOW_EMBEDDING_TIMEOUT_SECONDS 必须为正数")
            if self.siliconflow_embedding_max_retries < 0:
                errors.append("SILICONFLOW_EMBEDDING_MAX_RETRIES 必须 ≥ 0")
            if self.embedding_batch_size < 1:
                errors.append("EMBEDDING_BATCH_SIZE 必须 ≥ 1（它是每次 HTTP 里的文本条数）")
            allowed = SILICONFLOW_QWEN3_DIMENSIONS.get(self.embedding_model)
            if self.siliconflow_embedding_dimensions:
                if allowed is not None and self.siliconflow_embedding_dimensions not in allowed:
                    errors.append(
                        f"SILICONFLOW_EMBEDDING_DIMENSIONS({self.siliconflow_embedding_dimensions})"
                        f" 不在 {self.embedding_model} 支持的档位里 {list(allowed)}"
                    )
                if self.siliconflow_embedding_dimensions != self.embedding_dim:
                    # 请求降维到 N 却把 EMBEDDING_DIM 写成别的值 ⇒ 维度自检会在第一次
                    # 编码时炸（或更糟：写进维度不符的集合）。启动期就说清楚。
                    errors.append(
                        f"SILICONFLOW_EMBEDDING_DIMENSIONS"
                        f"({self.siliconflow_embedding_dimensions}) 必须等于 "
                        f"EMBEDDING_DIM({self.embedding_dim})；用原生维度请把它设为 0"
                    )

        if self.reranker_provider == "siliconflow" and self.reranker_enabled:
            # 只在真的启用重排时才要求 key：关掉重排就不该被一条无关的配置拦住启动。
            if not self.siliconflow_api_key:
                errors.append(
                    "RERANKER_PROVIDER=siliconflow 且 RERANKER_ENABLED=true 时必须配置 "
                    "SILICONFLOW_API_KEY"
                )
            if not self.siliconflow_base_url.startswith("http"):
                errors.append(
                    f"SILICONFLOW_BASE_URL 必须是完整 URL（当前 {self.siliconflow_base_url}）"
                )
            if self.siliconflow_rerank_timeout_seconds <= 0:
                errors.append("SILICONFLOW_RERANK_TIMEOUT_SECONDS 必须为正数")
            if self.siliconflow_rerank_max_retries < 0:
                errors.append("SILICONFLOW_RERANK_MAX_RETRIES 必须 ≥ 0")
            if self.siliconflow_rerank_max_documents < 1:
                errors.append("SILICONFLOW_RERANK_MAX_DOCUMENTS 必须 ≥ 1")
            if self.reranker_top_n < 1:
                errors.append("RERANKER_TOP_N 必须 ≥ 1")

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
            # 内存任务表 + 独立进程消费 = Worker 永远查不到任务（消息被 ack 丢弃，任务永久
            # 停在 PENDING）。本地调试允许，生产直接拒绝启动。
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
                # 同时出现在写白名单与全局黑名单里 = 配置自相矛盾，运行期会表现为
                # 「某些写工具时有时无」，不如启动即失败。
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


def _is_loopback(host: str) -> bool:
    """判断监听地址是否只在回环上。

    只看字面量、不做 DNS 解析：``localhost`` 在有些环境里会解析到非回环地址，而校验的
    目的恰恰是「不要相信环境」。写成独立函数是为了能被测试直接覆盖 —— 内联进
    ``validate_for_startup`` 的话，验证这条规则就得先构造一个非法配置，测试会变成
    对异常文案的断言。
    """
    return host.strip().lower() in {"127.0.0.1", "::1", "localhost"}


@lru_cache
def get_settings() -> Settings:
    """返回进程内单例配置（便于 FastAPI 依赖注入）。"""
    return Settings()


def apply_hf_endpoint(settings: Settings) -> str | None:
    """让 ``HF_ENDPOINT`` 真正对 ``huggingface_hub`` 生效，返回生效值（未配置则 ``None``）。

    这是个真踩过的坑（``docs/12-§13.1``）。``huggingface_hub`` 只认进程环境变量、
    不读 ``.env``，且在 import 期就把值固化进 ``ENDPOINT`` /
    ``HUGGINGFACE_CO_URL_TEMPLATE`` 两个常量；只有显式传 ``endpoint=`` 参数时
    ``hf_hub_url()`` 才会重写域名，所以「import 之后再设环境变量」完全不生效。

    而 ``app`` 自己会在 ``import app.main`` 期间就把 ``huggingface_hub`` 拉进来
    （``app.rag.chunking`` → ``langchain_text_splitters`` → ``transformers``），
    等 ``create_app()`` 执行时早已错过窗口期 —— 实测 ``os.environ`` 里明明有值，
    请求仍发往 ``huggingface.co``，表现为「首次加载模型卡 5 次重试 × 超时后才降级」。

    因此这里写环境变量之外，还同步改写上面那两个常量 —— 这是导入顺序不可控时
    唯一能确定性生效的办法。
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
